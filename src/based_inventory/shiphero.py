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

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests


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


class ShipHeroClient:
    def __init__(
        self, token: str, api_url: str = "https://public-api.shiphero.com/graphql"
    ) -> None:
        self.endpoint = api_url
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _execute(
        self, query: str, variables: dict[str, Any] | None = None, retries: int = 6
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = requests.post(
                    self.endpoint,
                    json={"query": query, "variables": variables or {}},
                    headers=self.headers,
                    timeout=60,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt < retries:
                    time.sleep(min(2**attempt * 2, 30))
                    continue
                raise RuntimeError(f"ShipHero network error after {retries} retries: {e}") from e
            payload = r.json() if r.content else {}
            if "errors" in payload:
                err = payload["errors"][0] if payload["errors"] else {}
                if err.get("code") == 30 and attempt < retries:
                    raw = str(err.get("time_remaining") or "60")
                    digits = "".join(c for c in raw if c.isdigit()) or "60"
                    time.sleep(min(int(digits) + 1, 90))
                    continue
                raise RuntimeError(f"ShipHero GraphQL errors: {payload['errors']}")
            r.raise_for_status()
            return payload
        raise RuntimeError(f"ShipHero retries exhausted: {last_exc}")

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
        now_dt = datetime.utcnow()
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

        now_iso = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
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
        max_pages: int = 50,
    ) -> list[dict[str, Any]]:
        """Fetch all orders for a single calendar day with line_items.

        Returns a list of order nodes, each with a `line_items.edges` list.
        Pagination via `order_date_from` advance + dedupe by order_number.

        day_iso_date format: 'YYYY-MM-DD'. The day window is
        [day 00:00:00, day+1 00:00:00).

        Note: each `line_items` connection nested in an orders query
        multiplies query complexity. Per-day scoping keeps the per-call
        cost under the 4004 credit cap.
        """
        from datetime import datetime, timedelta

        d = datetime.strptime(day_iso_date, "%Y-%m-%d")
        day_start = d.strftime("%Y-%m-%dT00:00:00")
        day_end = (d + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")

        query = """
        query($since: ISODateTime!, $until: ISODateTime!, $warehouse_id: String!) {
          orders(
            order_date_from: $since
            order_date_to: $until
            warehouse_id: $warehouse_id
          ) {
            data {
              edges {
                node {
                  order_number
                  shop_name
                  order_date
                  line_items {
                    edges { node { sku quantity } }
                  }
                }
              }
              pageInfo { hasNextPage }
            }
          }
        }
        """
        all_orders: list[dict[str, Any]] = []
        seen: set[str] = set()
        cur_since = day_start
        for _ in range(max_pages):
            payload = self._execute(
                query,
                {"since": cur_since, "until": day_end, "warehouse_id": warehouse_id},
            )
            edges = payload["data"]["orders"]["data"]["edges"]
            if not edges:
                break
            page_max_date = cur_since
            new = 0
            for edge in edges:
                n = edge["node"]
                key = n.get("order_number") or ""
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                new += 1
                od = n.get("order_date") or page_max_date
                if od > page_max_date:
                    page_max_date = od
                all_orders.append(n)
            if new == 0:
                break
            if not payload["data"]["orders"]["data"]["pageInfo"]["hasNextPage"]:
                break
            if page_max_date == cur_since:
                break
            cur_since = page_max_date
            time.sleep(0.2)
        return all_orders

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

        now_iso = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
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
