"""ShipHero Public API client.

Endpoint: https://public-api.shiphero.com/graphql
Auth: Bearer JWT (28-day access token; refresh via Auth0 refresh-token grant).

Capability quirks captured in `~/Obsidian/Based/wiki/concepts/shiphero-api.md`:
- Most queries return a connection where pagination cursor lives on
  `data.edges[].cursor`, NOT as a root `after` argument.
- `inventory_changes` returns OLDEST-first; tight `date_from` is the simplest
  way to get a recent slice.
- `purchase_orders` query at default page=100 with line_items costs ~10K
  credits, blowing the per-op cap of 4004; fetch line_items per-PO if needed.
- Kit-component shipments emit a duplicate `inventory_changes` event whose
  `reason` contains "kit sku"; dedupe to avoid double-counting velocity.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

_log = logging.getLogger(__name__)


def _midpoint_iso(from_iso: str, to_iso: str) -> str | None:
    """Return the ISO timestamp midway between two ISO timestamps.

    Returns None if the parse fails or the window is narrower than 1 minute
    (signal to caller to stop bisecting).
    """
    try:
        f = datetime.fromisoformat(from_iso.replace("Z", "+00:00")).replace(tzinfo=None)
        t = datetime.fromisoformat(to_iso.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
    if t - f < timedelta(minutes=1):
        return None
    mid = f + (t - f) / 2
    return mid.strftime("%Y-%m-%dT%H:%M:%S")


MERCHDROP_WAREHOUSE_ID = "V2FyZWhvdXNlOjExNzY2MQ=="

# ShipHero credit pool defaults (observed 2026-06-03 via extensions.throttling):
# max_available=4004, increment_rate=60 credits/sec. Used as fallbacks when a
# response omits the live quota.
_DEFAULT_CREDIT_REFILL_RATE = 60.0
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _quota_number(value: Any) -> float | None:
    """Read a non-negative number from ShipHero without trusting raw error text."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _credit_wait_hint(value: Any) -> float | None:
    """Parse ShipHero's `17 seconds` refill hint, including minute units."""
    if not isinstance(value, str | int | float):
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(seconds?|minutes?)?\s*", str(value), re.I)
    if not match:
        return None
    seconds = _quota_number(match.group(1))
    if seconds is None:
        return None
    return seconds * (60 if (match.group(2) or "").lower().startswith("minute") else 1)


def _response_type(response: requests.Response) -> str:
    """Classify the content type without echoing a provider header into logs."""
    media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media_type == "application/json" or media_type.endswith("+json"):
        return "json"
    return "non-json" if media_type else "unspecified"


def _credits_refill_wait(credits_remaining: int, floor: int, increment_rate: float) -> float:
    """Seconds to wait for the credit pool to refill back up to `floor`.

    Returns 0 when already at/above the floor, when pacing is disabled
    (floor <= 0), or when the refill rate is non-positive. Otherwise the
    deficit divided by the per-second refill rate. Keeping the pool above a
    floor between calls leaves headroom for other ShipHero consumers (the ops
    team, the ShipHero UI) instead of draining the shared pool to zero.
    """
    if floor <= 0 or increment_rate <= 0 or credits_remaining >= floor:
        return 0.0
    return (floor - credits_remaining) / increment_rate


_ORDER_SHIPPED_REASON = re.compile(r"order .*shipped", re.IGNORECASE)
# NOTE: When querying inventory_changes WITH a sku filter, kit-rollup events
# ("Inventory updated because its kit sku <X> was updated. Order #Y shipped.")
# are the ONLY signal that a kit sale depleted this component. INCLUDE them.
# (The earlier dedup filter would have been correct for a warehouse-wide
# query without a sku filter, where both the kit's event AND the component's
# rollup event would appear for the same sale.)


@dataclass(frozen=True)
class WarehouseStock:
    sku: str
    on_hand: int
    available: int
    allocated: int
    backorder: int
    reserve_inventory: int
    sell_ahead: int
    product_name: str
    is_kit: bool


@dataclass(frozen=True)
class KitDefinition:
    sku: str
    name: str
    components: tuple[tuple[str, int], ...]  # (component_sku, quantity)
    is_kit_build: bool


class OrdersCountChangedError(RuntimeError):
    """An order-date window changed while its pages were being read."""

    def __init__(self, before: int, after: int) -> None:
        super().__init__(
            f"ShipHero orders total_count changed during pagination: {before} -> {after}"
        )


class ShipHeroClient:
    def __init__(
        self,
        token: str,
        api_url: str = "https://public-api.shiphero.com/graphql",
        min_credits_floor: int = 0,
        max_pacing_sleep: float = 90.0,
    ) -> None:
        self.endpoint = api_url
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        # When > 0, after each call wait (proactively) until the live credit
        # pool refills back to this floor, so we sip the shared pool instead of
        # draining it to zero in bursts. 0 keeps the legacy drain-then-throttle
        # behavior. Sleeps are capped at max_pacing_sleep so a wrong reading
        # can't hang a run.
        self.min_credits_floor = min_credits_floor
        self.max_pacing_sleep = max_pacing_sleep

    def _execute(
        self, query: str, variables: dict[str, Any] | None = None, retries: int = 12
    ) -> dict[str, Any]:
        if type(retries) is not int or not 0 <= retries <= 12:
            raise ValueError("ShipHero retries must be between 0 and 12")
        for attempt in range(retries + 1):
            try:
                r = requests.post(
                    self.endpoint,
                    json={"query": query, "variables": variables or {}},
                    headers=self.headers,
                    timeout=60,
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempt < retries:
                    time.sleep(min(2**attempt * 2, 30))
                    continue
                raise RuntimeError(f"ShipHero network error after {retries} retries") from None

            status = r.status_code
            response_type = _response_type(r)
            if not 200 <= status < 300:
                if status == 429 and response_type != "non-json" and r.content:
                    try:
                        throttle_payload = r.json()
                    except ValueError:
                        throttle_payload = None
                    if isinstance(throttle_payload, dict):
                        errors = throttle_payload.get("errors")
                        first = errors[0] if isinstance(errors, list) and errors else None
                        if isinstance(first, dict) and first.get("code") == 30:
                            self._handle_credit_error(first, throttle_payload, attempt, retries)
                            continue
                if status in _RETRYABLE_HTTP_STATUSES and attempt < retries:
                    time.sleep(min(2**attempt * 2, 30))
                    continue
                raise RuntimeError(
                    f"ShipHero HTTP {status} ({response_type} response) after {attempt} retries"
                )

            if response_type == "non-json" or not r.content:
                if attempt < retries:
                    time.sleep(min(2**attempt * 2, 30))
                    continue
                raise RuntimeError(
                    f"ShipHero HTTP {status} invalid {response_type} response after {attempt} retries"
                )
            try:
                payload = r.json()
            except ValueError:
                if attempt < retries:
                    time.sleep(min(2**attempt * 2, 30))
                    continue
                raise RuntimeError(
                    f"ShipHero HTTP {status} invalid {response_type} response after {attempt} retries"
                ) from None
            if not isinstance(payload, dict):
                if attempt < retries:
                    time.sleep(min(2**attempt * 2, 30))
                    continue
                raise RuntimeError(
                    f"ShipHero HTTP {status} invalid {response_type} response after {attempt} retries"
                )
            if "errors" in payload:
                errors = payload["errors"]
                err = errors[0] if isinstance(errors, list) and errors else {}
                if not isinstance(err, dict):
                    err = {}
                if err.get("code") == 30:
                    self._handle_credit_error(err, payload, attempt, retries)
                    continue
                # Preserve the callers' distinct refill and window-splitting
                # signals without echoing any provider error details.
                messages = (
                    [
                        item["message"][:512].lower()
                        for item in errors
                        if isinstance(item, dict) and type(item.get("message")) is str
                    ]
                    if isinstance(errors, list)
                    else []
                )
                if any("not enough credits" in message for message in messages) and not any(
                    "requires too many credits" in message for message in messages
                ):
                    raise RuntimeError("ShipHero GraphQL not enough credits")
                if any("credits" in message for message in messages):
                    raise RuntimeError("ShipHero GraphQL credits limit")
                raise RuntimeError("ShipHero GraphQL error")
            self._pace_from_quota(payload)
            return payload
        raise RuntimeError("ShipHero retries exhausted")

    def _handle_credit_error(
        self, error: dict[str, Any], payload: dict[str, Any], attempt: int, retries: int
    ) -> None:
        """Wait for a refill on this request, or fail fast if it cannot fit."""
        quota = ((payload.get("extensions") or {}).get("throttling") or {}).get("user_quota") or {}
        required = _quota_number(error.get("required_credits"))
        remaining = _quota_number(error.get("remaining_credits"))
        maximum = _quota_number(quota.get("max_available"))
        if maximum is None:
            maximum = _quota_number(error.get("max_available"))
        message = error.get("message")
        message_says_oversized = (
            isinstance(message, str) and "requires too many credits" in message[:512].lower()
        )
        exceeds_quota = required is not None and maximum is not None and required > maximum
        if message_says_oversized or exceeds_quota:
            _log.warning(
                "ShipHero query exceeds credit cap: required=%s max=%s",
                f"{required:g}" if required is not None else "unknown",
                f"{maximum:g}" if maximum is not None else "unknown",
            )
            raise RuntimeError("ShipHero GraphQL credits limit")
        if attempt >= retries:
            raise RuntimeError("ShipHero GraphQL not enough credits")

        hint = _credit_wait_hint(error.get("time_remaining"))
        if required is not None and remaining is not None and maximum is not None:
            rate = _quota_number(quota.get("increment_rate")) or _DEFAULT_CREDIT_REFILL_RATE
            refill = max(required - remaining, 0) / rate
            hint = max(hint or 0, refill)
        # Other ShipHero consumers share this pool. A retry exactly at the
        # provider's refill estimate can lose every time under contention.
        wait = min(max(math.ceil(hint if hint is not None else 60) + 1, 15), 90)
        _log.warning(
            "ShipHero credit refill: required=%s remaining=%s max=%s wait=%ss attempt=%d/%d",
            f"{required:g}" if required is not None else "unknown",
            f"{remaining:g}" if remaining is not None else "unknown",
            f"{maximum:g}" if maximum is not None else "unknown",
            wait,
            attempt + 1,
            retries,
        )
        time.sleep(wait)

    def _pace_from_quota(self, payload: dict[str, Any]) -> None:
        """Proactively wait so the credit pool stays above min_credits_floor.

        Reads the live quota ShipHero returns on every response under
        extensions.throttling.user_quota. No-op when pacing is disabled
        (min_credits_floor <= 0) or the quota is absent.
        """
        if self.min_credits_floor <= 0:
            return
        quota = ((payload.get("extensions") or {}).get("throttling") or {}).get("user_quota") or {}
        remaining = quota.get("credits_remaining")
        if remaining is None:
            return
        rate = quota.get("increment_rate") or _DEFAULT_CREDIT_REFILL_RATE
        wait = _credits_refill_wait(int(remaining), self.min_credits_floor, float(rate))
        if wait > 0:
            time.sleep(min(wait, self.max_pacing_sleep))

    # -----------------------------------------------------------------------
    # Inventory snapshot
    # -----------------------------------------------------------------------
    def fetch_warehouse_stock(
        self,
        warehouse_id: str = MERCHDROP_WAREHOUSE_ID,
        updated_from: str = "2020-01-01T00:00:00",
        updated_to: str = "2099-01-01T00:00:00",
        max_calls: int = 64,
    ) -> list[WarehouseStock]:
        """Fetch every active warehouse_product via recursive bisection.

        ShipHero caps each call at 100 results regardless of filter and the
        `data` connection has no after/first cursor args. To enumerate beyond
        100 we split [updated_from, updated_to] until each window returns
        fewer than 100 rows. Dedup by SKU across windows so an entry that
        touches a boundary timestamp isn't lost.
        """
        query = """
        query($warehouse_id: String!, $updated_from: ISODateTime, $updated_to: ISODateTime) {
          warehouse_products(
            warehouse_id: $warehouse_id
            active: true
            updated_from: $updated_from
            updated_to: $updated_to
          ) {
            data {
              edges {
                node {
                  sku
                  on_hand
                  available
                  backorder
                  updated_at
                  product { name kit }
                }
              }
              pageInfo { hasNextPage }
            }
          }
        }
        """
        # Projection notes:
        #   - on_hand + available are needed for the availability ladder
        #     (OVERSOLD uses on_hand<0; the rest uses available).
        #   - backorder was re-added 2026-05-20 to feed the new backorder
        #     ladder. The CLAY1 case (14,796-unit backorder against
        #     available=0) was invisible without it.
        #   - allocated, reserve_inventory, sell_ahead remain dropped:
        #     unused downstream, and sell_ahead in particular is expensive
        #     (~15 credits/SKU on the per-SKU query — see fetch_warehouse_
        #     product_for_sku). The pool-drain incident that triggered the
        #     original drop was driven by `sell_ahead`, not by `backorder`,
        #     so re-adding `backorder` alone is the conservative move.
        #   - WarehouseStock keeps the other fields with default 0 so
        #     existing call sites stay untouched.

        def fetch(uf: str, ut: str) -> tuple[list[dict], bool]:
            payload = self._execute(
                query, {"warehouse_id": warehouse_id, "updated_from": uf, "updated_to": ut}
            )
            edges = payload["data"]["warehouse_products"]["data"]["edges"]
            saturated = (
                len(edges) >= 100
                and payload["data"]["warehouse_products"]["data"]["pageInfo"]["hasNextPage"]
            )
            return [e["node"] for e in edges], saturated

        seen: set[str] = set()
        all_rows: list[WarehouseStock] = []
        call_budget = [max_calls]

        def visit(uf: str, ut: str) -> None:
            if call_budget[0] <= 0:
                return
            call_budget[0] -= 1
            nodes, saturated = fetch(uf, ut)
            for n in nodes:
                if n["sku"] in seen:
                    continue
                seen.add(n["sku"])
                product = n.get("product") or {}
                all_rows.append(
                    WarehouseStock(
                        sku=n["sku"],
                        on_hand=n["on_hand"] or 0,
                        available=n["available"] or 0,
                        allocated=0,
                        backorder=n.get("backorder") or 0,
                        reserve_inventory=0,
                        sell_ahead=0,
                        product_name=product.get("name") or "",
                        is_kit=bool(product.get("kit")),
                    )
                )
            if not saturated:
                return
            mid = _midpoint_iso(uf, ut)
            if mid is None or mid in (uf, ut):
                # Window is too narrow to bisect further; accept truncation.
                return
            time.sleep(0.2)
            visit(uf, mid)
            visit(mid, ut)

        visit(updated_from, updated_to)
        return all_rows

    def fetch_warehouse_product_for_sku(
        self, sku: str, warehouse_id: str = MERCHDROP_WAREHOUSE_ID
    ) -> WarehouseStock | None:
        """Targeted fetch: a single SKU's warehouse_product row.

        Used to fill in component SKUs that aren't in the first-page
        warehouse_products result. Trimmed to the minimum projection
        (sku/on_hand/available + product) — matches the bulk query's
        2026-04-30 trim so backfill calls don't blow the credit pool
        when the bulk fetch already grazed the ceiling. Without this,
        weekly_snapshot's alias backfill silently fails on rate-limit
        and the resulting post claims live SKUs are 'not found in
        ShipHero' (e.g. CLAY1=4170 reported as missing on 2026-05-08).
        """
        query = """
        query($sku: String!, $warehouse_id: String!) {
          warehouse_products(sku: $sku, warehouse_id: $warehouse_id) {
            data {
              edges {
                node {
                  sku
                  on_hand
                  available
                  backorder
                  product { name kit }
                }
              }
            }
          }
        }
        """
        payload = self._execute(query, {"sku": sku, "warehouse_id": warehouse_id})
        edges = payload["data"]["warehouse_products"]["data"]["edges"]
        if not edges:
            return None
        n = edges[0]["node"]
        product = n.get("product") or {}
        return WarehouseStock(
            sku=n["sku"],
            on_hand=n["on_hand"] or 0,
            available=n["available"] or 0,
            allocated=0,
            backorder=n.get("backorder") or 0,
            reserve_inventory=0,
            sell_ahead=0,
            product_name=product.get("name") or "",
            is_kit=bool(product.get("kit")),
        )

    # -----------------------------------------------------------------------
    # Amazon FBA inventory (sparse — only Amazon-listed SKUs return rows)
    # -----------------------------------------------------------------------
    def fetch_fba_inventory(self, sku: str) -> list[dict[str, Any]]:
        """Return ShipHero's view of a SKU's Amazon FBA inventory.

        Each row represents one (marketplace_id, merchant_id) tuple. ShipHero's
        FbaInventory type only exposes 5 fields (id, legacy_id, quantity,
        marketplace_id, merchant_id) per a 2026-05-08 schema introspection;
        for the rich breakdown (fulfillable / inbound / reserved / unsellable)
        we'd need to talk to Amazon SP-API directly.

        Empirical note: most Based hero SKUs return an empty list here; only
        SKUs explicitly listed on Amazon FBA have rows. The bot uses this as
        a best-effort visibility hint, not a complete inventory picture.
        """
        query = """
        query($sku: String!) {
          products(sku: $sku) {
            data {
              edges {
                node {
                  sku
                  fba_inventory {
                    id
                    legacy_id
                    quantity
                    marketplace_id
                    merchant_id
                  }
                }
              }
            }
          }
        }
        """
        payload = self._execute(query, {"sku": sku})
        edges = payload["data"]["products"]["data"]["edges"]
        if not edges:
            return []
        out: list[dict[str, Any]] = []
        for edge in edges:
            n = edge.get("node") or {}
            for row in n.get("fba_inventory") or []:
                out.append(row)
        return out

    # -----------------------------------------------------------------------
    # Kit definitions (multi-channel bundles)
    # -----------------------------------------------------------------------
    def fetch_all_kits(
        self,
        updated_from: str = "2020-01-01T00:00:00",
        updated_to: str = "2099-01-01T00:00:00",
        max_calls: int = 32,
    ) -> list[KitDefinition]:
        """Fetch every product where kit=true via recursive bisection.

        Same approach as fetch_warehouse_stock: ShipHero caps each call at
        100 results so we split the [updated_from, updated_to] range until
        each window has < 100. Dedup by SKU across windows.
        """
        query = """
        query($updated_from: ISODateTime, $updated_to: ISODateTime) {
          products(has_kits: true, updated_from: $updated_from, updated_to: $updated_to) {
            data {
              edges {
                node {
                  sku
                  name
                  kit_build
                  updated_at
                  kit_components { sku quantity }
                }
              }
              pageInfo { hasNextPage }
            }
          }
        }
        """

        def fetch(uf: str, ut: str) -> tuple[list[dict], bool]:
            payload = self._execute(query, {"updated_from": uf, "updated_to": ut})
            edges = payload["data"]["products"]["data"]["edges"]
            saturated = (
                len(edges) >= 100 and payload["data"]["products"]["data"]["pageInfo"]["hasNextPage"]
            )
            return [e["node"] for e in edges], saturated

        seen: set[str] = set()
        all_kits: list[KitDefinition] = []
        call_budget = [max_calls]

        def visit(uf: str, ut: str) -> None:
            if call_budget[0] <= 0:
                return
            call_budget[0] -= 1
            nodes, saturated = fetch(uf, ut)
            for n in nodes:
                if n["sku"] in seen:
                    continue
                seen.add(n["sku"])
                comps = tuple((c["sku"], c["quantity"]) for c in (n.get("kit_components") or []))
                all_kits.append(
                    KitDefinition(
                        sku=n["sku"],
                        name=n["name"] or "",
                        components=comps,
                        is_kit_build=bool(n.get("kit_build")),
                    )
                )
            if not saturated:
                return
            mid = _midpoint_iso(uf, ut)
            if mid is None or mid in (uf, ut):
                return
            time.sleep(0.2)
            visit(uf, mid)
            visit(mid, ut)

        visit(updated_from, updated_to)
        return all_kits

    # -----------------------------------------------------------------------
    # Velocity from inventory_changes
    # -----------------------------------------------------------------------
    def fetch_sku_depletion(
        self,
        sku: str,
        date_from_iso: str,
        warehouse_id: str = MERCHDROP_WAREHOUSE_ID,
        max_pages: int = 50,
    ) -> tuple[int, float]:
        """Return (UNITS_DEPLETED, effective_window_days) for a SKU.

        - UNITS_DEPLETED: total order-shipped events' magnitude, dedup'd of
          kit-rollup duplicates.
        - effective_window_days: the actual time span of captured events,
          in days. When max_pages saturates on a high-velocity SKU, this is
          MUCH smaller than the requested window; the caller should divide
          UNITS_DEPLETED by effective_window_days, NOT by the requested
          window, to avoid undercounting velocity.

        Pagination: ShipHero returns inventory_changes oldest-first, default
        100 per call. We advance date_from to the last event's created_at
        on each iteration, dedupe by event id, stop on empty page or
        exhausted max_pages.
        """
        query = """
        query($sku: String!, $warehouse_id: String!, $date_from: ISODateTime!) {
          inventory_changes(
            sku: $sku
            warehouse_id: $warehouse_id
            date_from: $date_from
          ) {
            data {
              edges {
                node {
                  id
                  change_in_on_hand
                  reason
                  created_at
                }
              }
              pageInfo { hasNextPage }
            }
          }
        }
        """
        from datetime import datetime

        depleted = 0
        seen_ids: set[str] = set()
        first_event_dt: str | None = None
        last_event_dt: str | None = None
        cur_date_from = date_from_iso
        saturated = False
        for page_idx in range(max_pages):
            payload = self._execute(
                query,
                {
                    "sku": sku,
                    "warehouse_id": warehouse_id,
                    "date_from": cur_date_from,
                },
            )
            edges = payload["data"]["inventory_changes"]["data"]["edges"]
            if not edges:
                break
            new_events = 0
            last_created_at = cur_date_from
            for edge in edges:
                n = edge["node"]
                evt_id = n.get("id") or ""
                if evt_id and evt_id in seen_ids:
                    continue
                if evt_id:
                    seen_ids.add(evt_id)
                new_events += 1
                created_at = n.get("created_at") or last_created_at
                if first_event_dt is None or created_at < first_event_dt:
                    first_event_dt = created_at
                if last_event_dt is None or created_at > last_event_dt:
                    last_event_dt = created_at
                if created_at > last_created_at:
                    last_created_at = created_at
                reason = n.get("reason") or ""
                if not _ORDER_SHIPPED_REASON.search(reason):
                    continue
                # Include kit-rollup events when querying per-SKU; they ARE
                # the depletion signal from kit sales for this component.
                change = n.get("change_in_on_hand") or 0
                if change < 0:
                    depleted += -change
            if new_events == 0:
                break
            if not payload["data"]["inventory_changes"]["data"]["pageInfo"]["hasNextPage"]:
                break
            if last_created_at == cur_date_from:
                break
            if page_idx == max_pages - 1:
                saturated = True
            cur_date_from = last_created_at
            time.sleep(0.2)

        # Compute effective window. If saturated AND we have a span shorter
        # than the requested window, return the actual span as the effective
        # window so velocity math doesn't undercount.
        try:
            req_start = datetime.fromisoformat(date_from_iso.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            req_start = None
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)  # noqa: UP017
        requested_window_days = (
            max((now_dt - req_start).total_seconds() / 86400.0, 0.0) if req_start else 7.0
        )

        if saturated and first_event_dt and last_event_dt:
            try:
                first_dt = datetime.fromisoformat(first_event_dt.replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
                last_dt = datetime.fromisoformat(last_event_dt.replace("Z", "+00:00")).replace(
                    tzinfo=None
                )
                span_days = max((last_dt - first_dt).total_seconds() / 86400.0, 1 / 24.0)
                effective = min(span_days, requested_window_days)
                return depleted, effective
            except ValueError:
                pass
        return depleted, requested_window_days

    # -----------------------------------------------------------------------
    # Inbound POs (informational sidebar, not used in cover math per probe)
    # -----------------------------------------------------------------------
    def fetch_channel_mix(
        self,
        date_from_iso: str,
        warehouse_id: str = MERCHDROP_WAREHOUSE_ID,
    ) -> dict[str, int]:
        """Approximate channel mix by counting orders per shop_name.

        Returns: shop_name -> order count for the given window. We DON'T
        fetch line_items here (cost guard) so this is rough order-volume
        share, not unit-share. The known shop_names at Based:
          - "BASED"                         = TikTok Shop (~60-70% of vol)
          - "basedbodyworks.myshopify.com"  = Shopify     (~20%)
          - "Based Bodyworks Amazon"        = Amazon FBM  (~10%)

        Bisects the date window if a single call saturates at 100. Caps
        at 32 calls / ~3200 credits.
        """
        query = """
        query($since: ISODateTime!, $until: ISODateTime!, $warehouse_id: String!) {
          orders(
            order_date_from: $since
            order_date_to: $until
            warehouse_id: $warehouse_id
          ) {
            data {
              edges { node { order_number shop_name } }
              pageInfo { hasNextPage }
            }
          }
        }
        """

        from datetime import datetime as _dt

        now_iso = _dt.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")  # noqa: UP017
        counts: dict[str, int] = {}
        seen_orders: set[str] = set()
        call_budget = [32]

        def visit(uf: str, ut: str) -> None:
            if call_budget[0] <= 0:
                return
            call_budget[0] -= 1
            try:
                payload = self._execute(
                    query, {"since": uf, "until": ut, "warehouse_id": warehouse_id}
                )
            except RuntimeError as e:
                if "credits" in str(e).lower():
                    mid = _midpoint_iso(uf, ut)
                    if mid and mid != uf and mid != ut:
                        visit(uf, mid)
                        visit(mid, ut)
                return
            edges = payload["data"]["orders"]["data"]["edges"]
            for e in edges:
                n = e["node"]
                num = n.get("order_number") or ""
                if num and num in seen_orders:
                    continue
                if num:
                    seen_orders.add(num)
                shop = n.get("shop_name") or "(unknown)"
                counts[shop] = counts.get(shop, 0) + 1
            saturated = (
                len(edges) >= 100 and payload["data"]["orders"]["data"]["pageInfo"]["hasNextPage"]
            )
            if saturated:
                mid = _midpoint_iso(uf, ut)
                if mid and mid != uf and mid != ut:
                    time.sleep(0.2)
                    visit(uf, mid)
                    visit(mid, ut)

        visit(date_from_iso, now_iso)
        return counts

    def fetch_orders_for_day(
        self,
        day_iso_date: str,
        warehouse_id: str = MERCHDROP_WAREHOUSE_ID,
        max_pages: int = 2000,
    ) -> list[dict[str, Any]]:
        """Fetch all orders for a single calendar day with line_items.

        Uses the same complete cursor pagination as fetch_orders_window.
        The day window is [day 00:00:00, day+1 00:00:00) in UTC.
        """
        d = datetime.strptime(day_iso_date, "%Y-%m-%d")
        day_start = d.strftime("%Y-%m-%dT00:00:00")
        day_end = (d + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        return self.fetch_orders_window(
            day_start, warehouse_id=warehouse_id, max_pages=max_pages, until_iso=day_end
        )

    def fetch_inbound_outstanding_by_sku(
        self,
        po_date_from_iso: str = "2025-01-01T00:00:00",
    ) -> dict[str, dict[str, Any]]:
        """For each SKU appearing on a pending PO since `po_date_from_iso`,
        return the outstanding (not-yet-received) quantity, the most-recent
        po_date, and the most-recent ship_date (often null per probe).

        Returns: SKU -> {outstanding, latest_po_date, latest_ship_date,
        po_count, latest_po_number}.

        Cost guard: queries pending POs only (much smaller list than all
        POs); tightening fulfillment_status to "pending" empirically
        reduces the line_items connection cost enough to fit under the
        4004-credit per-op cap. Bisects by po_date if still saturated.
        """
        query = """
        query($since: ISODateTime!, $until: ISODateTime!) {
          purchase_orders(
            po_date_from: $since
            po_date_to: $until
            fulfillment_status: "pending"
          ) {
            data {
              edges {
                node {
                  po_number
                  po_date
                  ship_date
                  fulfillment_status
                  line_items {
                    edges {
                      node {
                        sku
                        quantity
                        quantity_received
                      }
                    }
                  }
                }
              }
              pageInfo { hasNextPage }
            }
          }
        }
        """

        out: dict[str, dict[str, Any]] = {}
        seen_pos: set[str] = set()
        call_budget = [16]

        def absorb(node: dict) -> None:
            po_number = node.get("po_number") or ""
            if po_number in seen_pos:
                return
            seen_pos.add(po_number)
            po_date = node.get("po_date")
            ship_date = node.get("ship_date")
            for ln in (node.get("line_items") or {}).get("edges", []):
                ln_node = ln.get("node") or {}
                sku = ln_node.get("sku")
                if not sku:
                    continue
                qty = int(ln_node.get("quantity") or 0)
                recv = int(ln_node.get("quantity_received") or 0)
                outstanding = max(qty - recv, 0)
                if outstanding <= 0:
                    continue
                entry = out.setdefault(
                    sku,
                    {
                        "outstanding": 0,
                        "po_count": 0,
                        "latest_po_date": None,
                        "latest_ship_date": None,
                        "latest_po_number": None,
                    },
                )
                entry["outstanding"] += outstanding
                entry["po_count"] += 1
                if po_date and (
                    entry["latest_po_date"] is None or po_date > entry["latest_po_date"]
                ):
                    entry["latest_po_date"] = po_date
                    entry["latest_po_number"] = po_number
                if ship_date and (
                    entry["latest_ship_date"] is None or ship_date > entry["latest_ship_date"]
                ):
                    entry["latest_ship_date"] = ship_date

        def visit(uf: str, ut: str) -> None:
            if call_budget[0] <= 0:
                return
            call_budget[0] -= 1
            try:
                payload = self._execute(query, {"since": uf, "until": ut})
            except RuntimeError as e:
                # Most common: rate-limit error 30 (line_items connection blew
                # the per-op cap). Bisect.
                if "credits" in str(e).lower():
                    mid = _midpoint_iso(uf, ut)
                    if mid and mid != uf and mid != ut:
                        visit(uf, mid)
                        visit(mid, ut)
                    return
                raise
            edges = payload["data"]["purchase_orders"]["data"]["edges"]
            for e in edges:
                absorb(e["node"])
            saturated = (
                len(edges) >= 100
                and payload["data"]["purchase_orders"]["data"]["pageInfo"]["hasNextPage"]
            )
            if saturated:
                mid = _midpoint_iso(uf, ut)
                if mid and mid != uf and mid != ut:
                    time.sleep(0.2)
                    visit(uf, mid)
                    visit(mid, ut)

        from datetime import datetime as _dt

        now_iso = _dt.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")  # noqa: UP017
        visit(po_date_from_iso, now_iso)
        return out

    def fetch_open_purchase_orders(self, po_date_from_iso: str) -> list[dict[str, Any]]:
        """List pending POs since the given date. Without line_items (cost guard).

        First-page only (same v0 pagination caveat as the other fetchers).
        """
        query = """
        query($since: ISODateTime!) {
          purchase_orders(po_date_from: $since, fulfillment_status: "pending") {
            data {
              edges {
                cursor
                node {
                  po_number
                  po_date
                  ship_date
                  arrived_at
                  warehouse_id
                  total_price
                }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        payload = self._execute(query, {"since": po_date_from_iso})
        return [e["node"] for e in payload["data"]["purchase_orders"]["data"]["edges"]]

    _ORDERS_PAGE_SIZE = 25
    _ORDERS_LINE_ITEMS_CAP = 15
    _ORDERS_LINE_ITEMS_MAX_PAGES = 20

    def fetch_orders_window(
        self,
        since_iso: str,
        warehouse_id: str = MERCHDROP_WAREHOUSE_ID,
        max_pages: int = 500,
        until_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch orders whose order_date is in [since_iso, until_iso], with line_items.

        Orders are paginated with ShipHero's cursor, since order_date is not
        necessarily sorted within a page. Both connection page sizes are
        explicit to keep estimated GraphQL credit cost below the account cap.

        High-volume caveat: a wide window (e.g. 48h) at Based's order rate spans
        many pages, so this issues sequential calls and can burn a meaningful
        slice of the credit pool per run.

        since_iso format: 'YYYY-MM-DDTHH:MM:SS' (UTC). When until_iso is None the
        upper bound defaults to now (UTC); pass an explicit until_iso to bound a
        chunk (e.g. a single day [day 00:00:00, day+1 00:00:00]) so a long
        multi-day backfill can be checkpointed chunk-by-chunk.
        """
        from datetime import datetime as _dt

        now_iso = until_iso or _dt.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")  # noqa: UP017
        filters = {"since": since_iso, "until": now_iso, "warehouse_id": warehouse_id}
        count_query = """
        query($since: ISODateTime!, $until: ISODateTime!, $warehouse_id: String!) {
          orders(order_date_from: $since, order_date_to: $until, warehouse_id: $warehouse_id) {
            data(first: 1) { total_count }
          }
        }
        """

        def count_orders() -> int:
            payload = self._execute(count_query, filters)
            reported = payload["data"]["orders"]["data"].get("total_count")
            if type(reported) is not int or reported < 0:
                raise RuntimeError("ShipHero orders invalid total_count")
            return reported

        query = (
            """
        query($since: ISODateTime!, $until: ISODateTime!, $warehouse_id: String!, $after: String) {
          orders(
            order_date_from: $since
            order_date_to: $until
            warehouse_id: $warehouse_id
          ) {
            data(first: """
            + str(self._ORDERS_PAGE_SIZE)
            + """, after: $after) {
              edges {
                node {
                  id
                  order_number
                  order_date
                  shop_name
                  line_items(first: """
            + str(self._ORDERS_LINE_ITEMS_CAP)
            + """) {
                    edges { node { id sku quantity } }
                    pageInfo { hasNextPage endCursor }
                  }
                }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        )

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = None
        expected_count = count_orders()
        for _ in range(max_pages):
            payload = self._execute(
                query,
                {**filters, "after": cursor},
            )
            connection = payload["data"]["orders"]["data"]
            edges = connection["edges"]
            page_info = connection["pageInfo"]
            if not edges:
                if page_info["hasNextPage"]:
                    raise RuntimeError(
                        "ShipHero orders pagination returned empty page with more data"
                    )
                break
            for e in edges:
                n = e["node"]
                order_id = n.get("id")
                if not isinstance(order_id, str) or not order_id:
                    raise RuntimeError("ShipHero orders pagination order id missing")
                if order_id in seen:
                    raise RuntimeError("ShipHero orders pagination repeated order id")
                seen.add(order_id)
                self._finish_order_lines(n)
                out.append(n)
            if not page_info["hasNextPage"]:
                break
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise RuntimeError("ShipHero orders pagination cursor missing")
            if next_cursor == cursor:
                raise RuntimeError("ShipHero orders pagination cursor did not advance")
            cursor = next_cursor
            time.sleep(0.2)
        else:
            raise RuntimeError(f"ShipHero orders pagination exceeded {max_pages} pages")
        final_count = count_orders()
        if final_count != expected_count:
            raise OrdersCountChangedError(expected_count, final_count)
        if len(out) != expected_count:
            raise RuntimeError(
                f"ShipHero orders total_count mismatch: fetched {len(out)}, expected {expected_count}"
            )
        return out

    def _finish_order_lines(self, order: dict[str, Any]) -> None:
        """Append line-item pages for an order whose first page is truncated."""
        lines = order["line_items"]
        page_info = lines["pageInfo"]
        seen_line_ids: set[str] = set()

        def check_ids(edges: list[dict[str, Any]]) -> None:
            for edge in edges:
                line_id = edge["node"].get("id")
                if not isinstance(line_id, str) or not line_id:
                    raise RuntimeError(
                        f"ShipHero line items missing line id for order {order['id']}"
                    )
                if line_id in seen_line_ids:
                    raise RuntimeError(
                        f"ShipHero line items repeated line id for order {order['id']}"
                    )
                seen_line_ids.add(line_id)

        check_ids(lines["edges"])
        if not page_info["hasNextPage"]:
            return
        query = (
            """
        query($order_id: String!, $line_after: String) {
          order(id: $order_id) {
            data {
              id
              line_items(first: """
            + str(self._ORDERS_LINE_ITEMS_CAP)
            + """, after: $line_after) {
                edges { node { id sku quantity } }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        """
        )
        cursor = page_info.get("endCursor")
        for _ in range(self._ORDERS_LINE_ITEMS_MAX_PAGES - 1):
            if not isinstance(cursor, str) or not cursor:
                raise RuntimeError(f"ShipHero line items cursor missing for order {order['id']}")
            payload = self._execute(query, {"order_id": order["id"], "line_after": cursor})
            order_data = payload["data"]["order"]["data"]
            if order_data["id"] != order["id"]:
                raise RuntimeError(f"ShipHero line items order id changed for order {order['id']}")
            lines_page = order_data["line_items"]
            if not lines_page["edges"]:
                raise RuntimeError(f"ShipHero line items empty page for order {order['id']}")
            check_ids(lines_page["edges"])
            lines["edges"].extend(lines_page["edges"])
            page_info = lines_page["pageInfo"]
            if not page_info["hasNextPage"]:
                return
            next_cursor = page_info.get("endCursor")
            if next_cursor == cursor:
                raise RuntimeError(
                    f"ShipHero line items cursor did not advance for order {order['id']}"
                )
            cursor = next_cursor
        raise RuntimeError(
            f"ShipHero line items exceeded {self._ORDERS_LINE_ITEMS_MAX_PAGES} pages "
            f"for order {order['id']}"
        )


# ---------------------------------------------------------------------------
# Helper: cluster kit definitions by component identity
# ---------------------------------------------------------------------------
def cluster_kits_by_components(
    kits: list[KitDefinition],
) -> dict[tuple[tuple[str, int], ...], list[KitDefinition]]:
    """Group kit SKUs that share the same component-set+quantities.

    The same logical bundle (e.g., Curly Duo) often exists as multiple SKUs
    across channels (Shopify, TikTok Shop, multi-pack variants). Grouping by
    sorted component tuples gives us the logical bundle identity.
    """
    clusters: dict[tuple[tuple[str, int], ...], list[KitDefinition]] = defaultdict(list)
    for kit in kits:
        key = tuple(sorted(kit.components))
        clusters[key].append(kit)
    return dict(clusters)
