"""Fridays 9am PST: post full inventory snapshot to Slack.

Source of truth: ShipHero (Merchdrop warehouse). Resolves the
AUDIT_LAYOUT product names to ShipHero SKUs via the BundleRegistry's
substring-fallback name matcher.

Tracks 23 products across 6 categories at the trusted-single level.
Bundles excluded; their cover is pinned by lowest component (per the
weekend-merch report).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from based_inventory.config import Config
from based_inventory.discontinued import DiscontinuedFilter
from based_inventory.jobs._common import run_job
from based_inventory.registry import _name_matches, build_registry
from based_inventory.shiphero import MERCHDROP_WAREHOUSE_ID, ShipHeroClient, WarehouseStock
from based_inventory.shiphero_auth import resolve_access_token
from based_inventory.slack import SlackClient, context, divider, header, section

logger = logging.getLogger(__name__)

LOW = 1000

# Amazon US marketplace ID; only marketplace currently in scope for FBA visibility.
US_MARKETPLACE_ID = "ATVPDKIKX0DER"

AUDIT_LAYOUT: list[tuple[str, list[str]]] = [
    ("Hair Care", ["Shampoo", "Conditioner", "Hair Elixir"]),
    ("Straight/Wavy Styling", ["Texture Powder", "Sea Salt Spray", "Pomade", "Hair Clay"]),
    (
        "Curly Styling",
        [
            "Leave-In Conditioner",
            "Curl Cream",
            "Curl Mousse",
            "Curl Gel",
            "Curl Refresh Spray",
        ],
    ),
    ("Body", ["Body Wash", "Body Lotion", "Deodorant"]),
    (
        "Skin",
        [
            "Daily Facial Cleanser",
            "Daily Facial Moisturizer",
            "Skin Revival Spray",
            "Under Eye Elixir",
            "Tallow Moisturizer",
        ],
    ),
    ("Accessories", ["Toiletry Bag", "Scalp Scrubber", "Wooden Hair Comb"]),
]

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
COMPONENTS_PATH = DATA_DIR / "set-components.json"
DISCONTINUED_PATH = DATA_DIR / "discontinued-skus.json"
ALIASES_PATH = DATA_DIR / "audit-aliases.json"


@dataclass(init=False)
class ProductLine:
    """One row in the weekly audit. Carries the three load-bearing inventory
    numbers from ShipHero so the renderer can tell whether a SKU is actually
    sellable (vs just physically present).

    - on_hand: physical units in warehouse (what ShipHero shows as inventory).
    - available: sellable / unallocated units (on_hand minus allocated minus
      reserved). This is the right risk signal: a SKU with 4,724 on_hand but
      0 available is fully promised to existing orders, not safe to promote.
    - backorder: queued customer demand against this SKU (units already owed
      to customers when we can't fulfill from current stock).

    Constructor accepts the legacy `qty=` kwarg for back-compat with older
    tests + call sites; if neither `on_hand` nor `qty` is supplied, defaults
    to 0. If `available` is omitted, it mirrors `on_hand` (matches the
    pre-2026-05-08 behavior where the renderer only knew about on_hand).
    """

    name: str
    on_hand: int
    sku: str | None
    affected_bundles: list[str]
    available: int
    backorder: int
    fba_qty: int | None
    fetch_error: bool

    def __init__(
        self,
        name: str,
        sku: str | None,
        affected_bundles: list[str],
        on_hand: int | None = None,
        available: int | None = None,
        backorder: int = 0,
        qty: int | None = None,
        fba_qty: int | None = None,
        fetch_error: bool = False,
    ) -> None:
        resolved_on_hand = on_hand if on_hand is not None else (qty if qty is not None else 0)
        self.name = name
        self.on_hand = resolved_on_hand
        self.sku = sku
        self.affected_bundles = affected_bundles
        self.available = available if available is not None else resolved_on_hand
        self.backorder = backorder
        self.fba_qty = fba_qty
        self.fetch_error = fetch_error

    @property
    def qty(self) -> int:
        """Back-compat alias. New code should use `on_hand`."""
        return self.on_hand


@dataclass(frozen=True)
class Resolved:
    """Result of resolving an AUDIT_LAYOUT name to ShipHero stock.

    `skus` lists every contributing physical SKU (single-element for
    direct matches; multi-element for aliased aggregates like
    "Tallow Moisturizer" = 50ml + 100ml). The three quantity fields
    (`on_hand`, `available`, `backorder`) are summed across those SKUs.
    `primary_sku` is the representative SKU shown in any UI that needs
    a single label (defaults to the highest-on_hand contributor).

    `qty` is kept as a back-compat alias for `on_hand`.
    """

    primary_sku: str
    on_hand: int
    skus: tuple[str, ...]
    available: int = 0
    backorder: int = 0

    @property
    def qty(self) -> int:
        """Back-compat alias. New code should use `on_hand`."""
        return self.on_hand


def _load_aliases(path: Path) -> dict[str, dict[str, Any]]:
    """Read audit-aliases.json. Missing/invalid file = empty mapping (silent no-op)."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    aliases = raw.get("aliases") or {}
    return {k: v for k, v in aliases.items() if isinstance(v, dict)}


def _emoji(available: int, backorder: int = 0, on_hand: int | None = None) -> str:
    """Risk emoji driven by `available` (sellable units).

    - ⛔ Oversold: on_hand < 0. Physical-negative stock; the most acute state.
    - 🚨 Critical: available <= 100, OR available <= 0 with queued backorder.
    - 🔴 / 🟠 / 🟡: standard ladder on `available`.
    - 📊 1K-5K, 🟢 >5K: healthy bands.

    Pre-2026-05-08 this took a single `qty` arg that meant `on_hand`, which
    let lines like "Conditioner: 4,724" look healthy when available was 0
    and 6K units were backordered. Switching the ladder to `available`
    fixes that. `on_hand` is still consulted for the ⛔ case so we don't
    miss physical-oversold situations.
    """
    if on_hand is not None and on_hand < 0:
        return "⛔"
    if available <= 0 and backorder > 0:
        return "🚨"
    if available <= 100:
        return "🚨"
    if available <= 500:
        return "🔴"
    if available <= 750:
        return "🟠"
    if available <= 1000:
        return "🟡"
    if available <= 5000:
        return "📊"
    return "🟢"


# Risk threshold for triggering the expanded per-row format. Matches the
# 🟡 HEADS UP boundary in quantity_alerts.THRESHOLDS; a SKU that would
# trigger an alert there gets full detail here too.
RISK_AVAILABLE_LOW = 1000


def _is_risk(line: ProductLine) -> bool:
    """A row is 'at risk' (expanded format) when any of:
    - available is at or below the low-stock threshold,
    - backorder queue exists (any positive value),
    - on_hand is physically negative (oversold).
    """
    return (
        line.available <= RISK_AVAILABLE_LOW
        or line.backorder > 0
        or line.on_hand < 0
    )


def _render_line(line: ProductLine) -> str:
    if line.fetch_error:
        return f"⚠️ {line.name}: ShipHero lookup failed (retry next run)"
    if line.sku is None:
        return f"❓ {line.name}: not found in ShipHero"

    emoji = _emoji(line.available, line.backorder, line.on_hand)

    if _is_risk(line):
        # Expanded: show the full stock picture so a reader can tell whether
        # there's real sellable inventory vs. just on_hand that's already
        # spoken for. Format matches the brief: `🚨 Conditioner: 4,724 on
        # hand · 0 available · 6,080 backordered`.
        parts = [
            f"*{line.on_hand:,}* on hand",
            f"*{line.available:,}* available",
        ]
        if line.backorder > 0:
            parts.append(f"*{line.backorder:,}* backordered")
        text = f"{emoji} {line.name}: " + "  ·  ".join(parts)
    else:
        # Healthy: compact; lead with sellable (`available`), not on_hand.
        text = f"{emoji} {line.name}: *{line.available:,}* available"

    if line.fba_qty is not None:
        text += f"  (🅰️  FBA: *{line.fba_qty:,}*)"
    return text


def build_snapshot_blocks(
    sections: list[tuple[str, list[ProductLine]]], date_str: str
) -> list[dict[str, Any]]:
    total = sum(len(lines) for _, lines in sections)
    blocks: list[dict[str, Any]] = [
        header("📦 Weekly Inventory Audit"),
        section(
            f"Tracking *{total}* products at *single-SKU level* (ShipHero source of truth)\n"
            f"Bundles excluded; constrained by lowest component  |  🗓️ {date_str}"
        ),
        divider(),
    ]

    for category, lines in sections:
        body = "*" + category + "*\n" + "\n".join(_render_line(line) for line in lines)
        blocks.append(section(body))

    blocks.append(divider())
    blocks.append(
        context(
            "Tiers driven by *available* (sellable units after allocation), not on_hand. "
            "🟢 5K+  ·  📊 1K-5K  ·  🟡 ≤1K  ·  🟠 ≤750  ·  🔴 ≤500  ·  🚨 ≤100  ·  "
            "⛔ Oversold (physical-negative).  At-risk rows expand to on-hand · available · "
            "backordered so you can tell when on-hand is already promised to existing orders.  "
            "·  source: ShipHero (Merchdrop)  ·  🅰️  FBA = Amazon US-marketplace on-hand "
            "(only shown for SKUs with FBA listings; full FBA breakdown requires Amazon SP-API)"
        )
    )
    return blocks


def _resolve_to_stock(
    name: str,
    by_name: dict[str, list[WarehouseStock]],
    by_sku: dict[str, WarehouseStock],
    bundle_skus: frozenset[str],
    discontinued: DiscontinuedFilter,
    aliases: dict[str, dict[str, Any]],
) -> Resolved | None:
    """Resolve an AUDIT_LAYOUT name to a Resolved stock summary.

    Resolution order:
    1. Explicit alias from `audit-aliases.json` — pin to a single SKU
       or aggregate across multiple SKUs. Aliases bypass kit / bundle /
       discontinued filters because Avi has explicitly chosen the SKU.
    2. Fuzzy `_name_matches` walk: iterate ranked candidates and return
       the first that passes is_kit / bundle / discontinued filters.
       Earlier behavior bailed to None as soon as the top match failed
       a filter (e.g. legacy V1 Scalp Scrubber mis-flagged is_kit=True
       killed the lookup even though V2 single was right behind it).

    Returns None only when the name has no alias AND every fuzzy
    candidate is filtered out.
    """
    alias = aliases.get(name)
    if alias:
        if "sku" in alias:
            stock = by_sku.get(alias["sku"])
            if stock is not None:
                return Resolved(
                    primary_sku=stock.sku,
                    on_hand=stock.on_hand,
                    available=stock.available,
                    backorder=stock.backorder,
                    skus=(stock.sku,),
                )
        if "skus" in alias:
            members = [by_sku[s] for s in alias["skus"] if s in by_sku]
            if members:
                # Pick highest-on_hand SKU as the primary label; sum the rest
                # across on_hand, available, and backorder so aggregated rows
                # (e.g. Tallow 50ml + 100ml) reflect the true total picture.
                primary = max(members, key=lambda s: s.on_hand)
                return Resolved(
                    primary_sku=primary.sku,
                    on_hand=sum(s.on_hand for s in members),
                    available=sum(s.available for s in members),
                    backorder=sum(s.backorder for s in members),
                    skus=tuple(s.sku for s in members),
                )
        # Alias present but its SKUs aren't in this warehouse: fall through
        # to fuzzy match rather than silently lying with on_hand=0.

    for candidate in _name_matches(name, by_name):
        if candidate.is_kit:
            continue
        if candidate.sku in bundle_skus:
            continue
        if discontinued.should_skip(candidate.sku, candidate.product_name):
            continue
        return Resolved(
            primary_sku=candidate.sku,
            on_hand=candidate.on_hand,
            available=candidate.available,
            backorder=candidate.backorder,
            skus=(candidate.sku,),
        )
    return None


def _affected_bundle_names(skus: tuple[str, ...], registry) -> list[str]:
    """Bundles whose components include any of `skus` (union across aggregates)."""
    skus_set = set(skus)
    out: list[str] = []
    for entry in registry.bundles:
        if any(c[0] in skus_set for c in entry.components_resolved):
            out.append(entry.bundle_name or entry.bundle_sku)
    return sorted(set(out))


def _run(cfg: Config) -> None:
    access_token = resolve_access_token(
        refresh_token=cfg.shiphero_refresh_token,
        fallback_access_token=cfg.shiphero_access_token,
    )
    client = ShipHeroClient(token=access_token, api_url=cfg.shiphero_api_url)
    discontinued = DiscontinuedFilter(DISCONTINUED_PATH)
    aliases = _load_aliases(ALIASES_PATH)
    slack = SlackClient(cfg.slack_bot_token, cfg.slack_channel, dry_run=cfg.dry_run)

    stock = client.fetch_warehouse_stock(warehouse_id=MERCHDROP_WAREHOUSE_ID)
    kits = client.fetch_all_kits()

    # Fill in component SKUs missing from page-1 fetch (so name match has them).
    component_skus = {c[0] for k in kits for c in k.components}
    known = {s.sku for s in stock}
    for sku in sorted(component_skus - known):
        try:
            row = client.fetch_warehouse_product_for_sku(sku, MERCHDROP_WAREHOUSE_ID)
            if row is not None:
                stock.append(row)
        except RuntimeError:
            continue

    # Pull aliased SKUs that aren't returned by warehouse_products (zero on_hand
    # rows can be excluded from the bulk paginated query). Without this, an
    # alias pinning to a 0-stock SKU would fall through to fuzzy match instead
    # of correctly reporting 0.
    aliased_skus: set[str] = set()
    for entry in aliases.values():
        if "sku" in entry:
            aliased_skus.add(entry["sku"])
        if "skus" in entry:
            aliased_skus.update(entry["skus"])
    backfill_failures: set[str] = set()
    for sku in sorted(aliased_skus - known):
        try:
            row = client.fetch_warehouse_product_for_sku(sku, MERCHDROP_WAREHOUSE_ID)
            if row is not None:
                stock.append(row)
            else:
                # Targeted query returned no edges — SKU genuinely doesn't
                # exist in this warehouse. Distinct from a fetch failure.
                logger.warning("Alias backfill: SKU %s has no row in ShipHero", sku)
        except RuntimeError as exc:
            # Rate-limit / network / GraphQL error. Do NOT silently swallow:
            # mark this SKU so downstream resolution can render 'fetch failed'
            # instead of falsely claiming the product doesn't exist (which is
            # what bit the 2026-05-08 run — CLAY1 has on_hand=4170 but the
            # post said 'not found in ShipHero').
            backfill_failures.add(sku)
            logger.warning("Alias backfill failed for SKU %s: %s", sku, exc)

    registry = build_registry(kits, stock, COMPONENTS_PATH)

    by_name: dict[str, list[WarehouseStock]] = {}
    by_sku: dict[str, WarehouseStock] = {}
    for s in stock:
        by_name.setdefault((s.product_name or "").strip(), []).append(s)
        by_sku.setdefault(s.sku, s)

    # Amazon FBA quantities are queried per resolved SKU. Most SKUs return
    # an empty list (only SKUs explicitly listed on Amazon FBA have rows).
    # Only US marketplace is summed; non-US (CA, MX) is currently out of scope.
    def _fba_qty_for(skus: tuple[str, ...]) -> int | None:
        total = 0
        any_row = False
        for sku in skus:
            try:
                rows = client.fetch_fba_inventory(sku)
            except RuntimeError:
                continue
            for r in rows:
                if r.get("marketplace_id") != US_MARKETPLACE_ID:
                    continue
                any_row = True
                total += int(r.get("quantity") or 0)
        return total if any_row else None

    def _alias_had_fetch_error(name: str) -> bool:
        """True if this name's alias points to SKUs that all failed to
        load this run (rate-limit / network). Distinguishes 'we couldn't
        check' from 'this product genuinely doesn't exist'."""
        alias = aliases.get(name)
        if not alias:
            return False
        alias_skus: list[str] = []
        if "sku" in alias:
            alias_skus.append(alias["sku"])
        if "skus" in alias:
            alias_skus.extend(alias["skus"])
        if not alias_skus:
            return False
        # If every aliased SKU failed to load AND none made it into by_sku,
        # the alias couldn't resolve through no fault of the data itself.
        return all(s in backfill_failures and s not in by_sku for s in alias_skus)

    sections: list[tuple[str, list[ProductLine]]] = []
    for category, names in AUDIT_LAYOUT:
        lines: list[ProductLine] = []
        for name in names:
            resolved = _resolve_to_stock(
                name, by_name, by_sku, registry.bundle_skus, discontinued, aliases
            )
            if resolved is None:
                lines.append(
                    ProductLine(
                        name=name,
                        qty=0,
                        sku=None,
                        affected_bundles=[],
                        fetch_error=_alias_had_fetch_error(name),
                    )
                )
                continue
            lines.append(
                ProductLine(
                    name=name,
                    on_hand=resolved.on_hand,
                    available=resolved.available,
                    backorder=resolved.backorder,
                    sku=resolved.primary_sku,
                    affected_bundles=_affected_bundle_names(resolved.skus, registry),
                    fba_qty=_fba_qty_for(resolved.skus),
                )
            )
        sections.append((category, lines))

    date_str = time.strftime("%b %d, %Y")
    blocks = build_snapshot_blocks(sections, date_str)
    fallback = f"📦 Weekly Inventory Audit: {date_str}"
    slack.post_message(fallback, blocks)


def main() -> None:
    run_job("weekly_snapshot", _run)


if __name__ == "__main__":
    main()
