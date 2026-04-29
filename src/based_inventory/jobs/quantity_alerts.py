"""Daily 7am Pacific: scan ShipHero inventory, post tier-escalation alerts to Slack.

Source of truth: ShipHero (Merchdrop warehouse). Shopify is no longer
trusted for inventory because it shows ~20% of unified channel mix.

Tiers (worst-first):
  🚨🚨 OVERSOLD       on_hand < 0    — already owe customers units
  🚨   CRITICAL        on_hand <= 100
  🔴   LOW STOCK       on_hand <= 500
  🟠   WARNING         on_hand <= 750
  🟡   HEADS UP        on_hand <= 1000

Live-SKU filter (DiscontinuedFilter) removes test / cruft / EOL SKUs
before any tier check; otherwise alerts on Tallow Moisturizer-style
deliberate run-downs would generate noise.

Each alert annotates weeks-of-cover + velocity-per-day computed from
ShipHero inventory_changes (~5 pages cap, kit-rollup events INCLUDED;
hero SKUs saturate the cap and effective-window scaling kicks in).

Bundle-affected list comes from BundleRegistry: any bundle whose
components include the at-risk SKU. Source-of-truth for bundle definitions
is ShipHero kit_components, supplemented by data/set-components.json
for Shopify-website bundles ShipHero doesn't model.

Dedup: AlertState's quantity_tiers is now keyed by SKU (was Shopify product
title). On first run after the rewire, clear the state file or accept
a one-time burst of "first cross" alerts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from based_inventory.config import Config
from based_inventory.discontinued import DiscontinuedFilter
from based_inventory.inventory import compute_sku_cover
from based_inventory.jobs._common import run_job
from based_inventory.registry import build_registry
from based_inventory.shiphero import MERCHDROP_WAREHOUSE_ID, ShipHeroClient
from based_inventory.shiphero_auth import resolve_access_token
from based_inventory.slack import SlackClient, context, divider, header, section
from based_inventory.state import AlertState

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
COMPONENTS_PATH = DATA_DIR / "set-components.json"
DISCONTINUED_PATH = DATA_DIR / "discontinued-skus.json"

# OVERSOLD uses tier sentinel -1 so it sorts as worst.
OVERSOLD_TIER = -1

# Threshold ladder (positive on_hand). Same units as legacy version so
# Slack readers see familiar labels.
THRESHOLDS: list[tuple[int, str]] = [
    (100, "🚨 CRITICAL"),
    (500, "🔴 LOW STOCK"),
    (750, "🟠 WARNING"),
    (1000, "🟡 HEADS UP"),
]
OVERSOLD_LABEL = "🚨🚨 OVERSOLD"

# Velocity sourcing knobs. Per-SKU max page cap keeps total run time bounded;
# effective-window scaling in fetch_sku_depletion handles the saturation case.
VELOCITY_WINDOW_DAYS = 7
VELOCITY_MAX_PAGES = 5


@dataclass
class Alert:
    label: str
    tier: int
    sku: str
    product_name: str
    on_hand: int
    velocity_per_day: float
    weeks_of_cover: float
    affected_bundles: list[str]
    inbound_outstanding: int = 0
    inbound_po_count: int = 0
    inbound_latest_po_date: str | None = None
    inbound_latest_ship_date: str | None = None


def _tier_for(on_hand: int) -> tuple[int, str] | None:
    if on_hand < 0:
        return OVERSOLD_TIER, OVERSOLD_LABEL
    for threshold, label in THRESHOLDS:
        if on_hand <= threshold:
            return threshold, label
    return None


def _format_cover(weeks: float) -> str:
    """Render weeks_of_cover for the alert annotation. Sub-week values
    show 2 decimals so 0.4w doesn't get rounded to 0.0w."""
    if weeks >= 9999:
        return "∞ (no observed depletion)"
    if weeks < 1:
        return f"{weeks:.2f}w"
    return f"{weeks:.1f}w"


def _format_channel_mix(counts: dict[str, int]) -> str | None:
    """Render the recent channel mix as 'TTS 70% / Shopify 20% / Amazon 10%'.
    Returns None if no orders observed."""
    total = sum(counts.values())
    if total == 0:
        return None
    label_map = {
        "BASED": "TTS",
        "basedbodyworks.myshopify.com": "Shopify",
        "Based Bodyworks Amazon": "Amazon",
    }
    pieces = []
    for shop_name, count in sorted(counts.items(), key=lambda x: -x[1]):
        pct = count * 100 / total
        if pct < 1:
            continue
        label = label_map.get(shop_name, shop_name)
        pieces.append(f"{label} {pct:.0f}%")
    return " / ".join(pieces)


def build_blocks(alerts: list[Alert], channel_mix_summary: str | None = None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        header("⚡ Inventory Alert"),
        divider(),
    ]

    for a in alerts:
        text_lines = [
            f"{a.label}  •  *{a.product_name or a.sku}*",
            f"📦  *{a.on_hand:,}* on hand"
            + (" (already owe customers units)" if a.on_hand < 0 else ""),
        ]
        if a.velocity_per_day > 0:
            text_lines.append(
                f"⏱️  {_format_cover(a.weeks_of_cover)} cover at {a.velocity_per_day:.0f}/day velocity"
            )
        elif a.on_hand >= 0:
            text_lines.append("⏱️  no recent depletion observed")
        if a.inbound_outstanding > 0:
            eta_bits = []
            if a.inbound_latest_ship_date:
                eta_bits.append(f"latest ship_date {a.inbound_latest_ship_date[:10]}")
            elif a.inbound_latest_po_date:
                eta_bits.append(f"latest PO {a.inbound_latest_po_date[:10]}, no ship_date")
            else:
                eta_bits.append("no ETA on file")
            text_lines.append(
                f"📥  *{a.inbound_outstanding:,}* inbound across "
                f"{a.inbound_po_count} pending PO{'s' if a.inbound_po_count != 1 else ''}"
                f" ({eta_bits[0]})"
            )
        if a.affected_bundles:
            preview = ", ".join(a.affected_bundles[:5])
            more = f" +{len(a.affected_bundles) - 5} more" if len(a.affected_bundles) > 5 else ""
            text_lines.append(f"⚠️  Bottleneck for: _{preview}{more}_")
        text_lines.append(f"`{a.sku}`")
        blocks.append(section("\n".join(text_lines)))

    blocks.append(divider())
    ts = time.strftime("%b %d, %I:%M %p PST", time.gmtime(time.time() - 7 * 3600))
    footer = f"🕐  {ts}  ·  source: ShipHero (Merchdrop)"
    if channel_mix_summary:
        footer += f"  ·  last 7d channel mix: {channel_mix_summary}"
    blocks.append(context(footer))
    return blocks


def _affected_bundle_names(sku: str, registry) -> list[str]:
    """Return bundle names whose components include this SKU."""
    out: list[str] = []
    for entry in registry.bundles:
        if any(c[0] == sku for c in entry.components_resolved):
            out.append(entry.bundle_name or entry.bundle_sku)
    return sorted(set(out))


def _run(cfg: Config) -> None:
    access_token = resolve_access_token(
        refresh_token=cfg.shiphero_refresh_token,
        fallback_access_token=cfg.shiphero_access_token,
    )
    client = ShipHeroClient(token=access_token, api_url=cfg.shiphero_api_url)

    discontinued = DiscontinuedFilter(DISCONTINUED_PATH)
    state = AlertState.load(cfg.state_path)
    slack = SlackClient(cfg.slack_bot_token, cfg.slack_channel, dry_run=cfg.dry_run)

    stock = client.fetch_warehouse_stock(warehouse_id=MERCHDROP_WAREHOUSE_ID)
    kits = client.fetch_all_kits()

    # Fill in component SKUs missing from the page-1 fetch (so bundle
    # math sees them). One per-SKU lookup each.
    component_skus = {c[0] for k in kits for c in k.components}
    known = {s.sku for s in stock}
    for sku in sorted(component_skus - known):
        try:
            row = client.fetch_warehouse_product_for_sku(sku, MERCHDROP_WAREHOUSE_ID)
            if row is not None:
                stock.append(row)
        except RuntimeError:
            continue

    registry = build_registry(kits, stock, COMPONENTS_PATH)

    # Scope candidates to KIT COMPONENT SKUs only. ShipHero's catalog has
    # hundreds of zombie / test / legacy SKUs and even the discontinued
    # filter can't catch them all by heuristic. The component set
    # (~30 SKUs) is the natural whitelist: these are the real physical
    # singles that bundles depend on, and they're what we care about
    # alerting on. Non-component SKUs we want to track (toiletry bag GWP,
    # standalone accessories) get added to a future tracked-skus.json
    # if needed; not in scope for v0.
    candidates = [
        s for s in stock
        if not s.is_kit
        and s.sku not in registry.bundle_skus
        and s.sku in component_skus
        and not discontinued.should_skip(s.sku, s.product_name)
    ]

    # Velocity sourcing for each candidate.
    since = time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(time.time() - VELOCITY_WINDOW_DAYS * 86400),
    )
    depletion: dict[str, int] = {}
    eff_windows: dict[str, float] = {}
    for s in candidates:
        try:
            d, eff = client.fetch_sku_depletion(
                sku=s.sku,
                date_from_iso=since,
                warehouse_id=MERCHDROP_WAREHOUSE_ID,
                max_pages=VELOCITY_MAX_PAGES,
            )
            depletion[s.sku] = d
            eff_windows[s.sku] = eff
        except RuntimeError:
            depletion[s.sku] = 0
            eff_windows[s.sku] = float(VELOCITY_WINDOW_DAYS)

    sku_cover = compute_sku_cover(
        candidates,
        depletion,
        window_days=VELOCITY_WINDOW_DAYS,
        registry=registry,
        effective_window_by_sku=eff_windows,
    )

    # Inbound visibility: fetch pending POs once and index by SKU. Each
    # alert annotates outstanding inbound qty + most-recent po_date (and
    # ship_date if set).
    try:
        inbound = client.fetch_inbound_outstanding_by_sku(
            po_date_from_iso="2025-01-01T00:00:00"
        )
    except RuntimeError:
        inbound = {}

    alerts: list[Alert] = []
    new_tiers: dict[str, int] = {}
    for s in candidates:
        tier_info = _tier_for(s.on_hand)
        if tier_info is None:
            state.clear_tier(s.sku)
            continue
        tier_value, label = tier_info
        new_tiers[s.sku] = tier_value
        if not state.crosses_lower_tier(s.sku, tier_value):
            continue
        cover = sku_cover.get(s.sku)
        inb = inbound.get(s.sku) or {}
        alerts.append(
            Alert(
                label=label,
                tier=tier_value,
                sku=s.sku,
                product_name=s.product_name,
                on_hand=s.on_hand,
                velocity_per_day=cover.velocity_per_day if cover else 0.0,
                weeks_of_cover=cover.weeks_of_cover if cover else 0.0,
                affected_bundles=_affected_bundle_names(s.sku, registry),
                inbound_outstanding=inb.get("outstanding", 0),
                inbound_po_count=inb.get("po_count", 0),
                inbound_latest_po_date=inb.get("latest_po_date"),
                inbound_latest_ship_date=inb.get("latest_ship_date"),
            )
        )

    state.quantity_tiers = new_tiers
    state.save(cfg.state_path)

    if not alerts:
        return

    # Sort: oversold first (most negative on_hand first), then by tier ASC
    # (CRITICAL before HEADS UP), then by on_hand ASC within tier.
    alerts.sort(key=lambda a: (a.tier, a.on_hand))

    # Channel mix snapshot for footer (last 7 days). Cheap, single optional
    # query; falls back to no annotation on error.
    try:
        channel_counts = client.fetch_channel_mix(date_from_iso=since)
        channel_summary = _format_channel_mix(channel_counts)
    except RuntimeError:
        channel_summary = None

    blocks = build_blocks(alerts, channel_mix_summary=channel_summary)
    fallback = f"⚡ Inventory Alert: {len(alerts)} SKU(s) below threshold"
    slack.post_message(fallback, blocks)


def main() -> None:
    run_job("quantity_alerts", _run)


if __name__ == "__main__":
    main()
