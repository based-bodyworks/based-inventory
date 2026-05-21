"""Daily 7am Pacific: scan ShipHero inventory, post tier-escalation alerts to Slack.

Source of truth: ShipHero (Merchdrop warehouse). Shopify is no longer
trusted for inventory because it shows ~20% of unified channel mix.

Two ladders run in parallel per SKU; an alert posts if either crosses worse:

Availability ladder (uses `available` = on_hand - allocated, except OVERSOLD
which uses on_hand to catch physical-negative stock):
  🚨🚨 OVERSOLD       on_hand < 0    — already owe customers physical units
  🚨   CRITICAL        available <= 100
  🔴   LOW STOCK       available <= 500
  🟠   WARNING         available <= 750
  🟡   HEADS UP        available <= 1000

Backorder ladder (uses `backorder` = queued customer demand against the SKU,
gated on `available <= backorder` so stale ShipHero counters don't fire):
  📥📥📥 BACKORDER MASSIVE   backorder >= 10,000
  📥📥  BACKORDER CRITICAL  backorder >= 5,000
  📥    BACKORDER ALARM     backorder >= 1,000
  📥    BACKORDER NOTICE    backorder >= 100

The availability ladder catches the "can a buyer take another unit" question;
the backorder ladder catches the "how deep is the demand hole" question. A
SKU like CLAY1 at on_hand=312, allocated=312, backorder=14,796 looked fine
to the prior on_hand-only logic (LOW STOCK, even an improvement from prior
CRITICAL=45), while actually catastrophic. Surfacing `available=0` AND a
14,796-unit backorder queue makes the real state visible.

The backorder gate exists because ShipHero's `warehouse_products.backorder`
is sticky after restock (see `_backorder_is_alertable` docstring for the
2026-05-20 CRS evidence). Without it the v3 release fired backorder alerts
on 10 healthy SKUs in the first burst (Sea Salt Spray, Pomade, Shampoo,
etc.) where 70-107K units of available stock dwarfed the residual backorder
counter from prior depletion events.

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

Dedup: AlertState carries two parallel SKU->tier maps (quantity_tiers,
backorder_tiers). Each ladder dedups independently; the alert fires if
EITHER ladder crosses to a worse bucket since the last run. The schema
bump v2->v3 (2026-05-20) clears prior on_hand-keyed quantity_tiers so the
first run after deploy re-evaluates all SKUs fresh.
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

# Availability ladder. Threshold value = upper-bound (lower = worse). Same
# units as legacy version so Slack readers see familiar labels.
THRESHOLDS: list[tuple[int, str]] = [
    (100, "🚨 CRITICAL"),
    (500, "🔴 LOW STOCK"),
    (750, "🟠 WARNING"),
    (1000, "🟡 HEADS UP"),
]
OVERSOLD_LABEL = "🚨🚨 OVERSOLD"

# Backorder ladder. Threshold value = lower-bound (higher = worse). Walked
# descending so the worst applicable bucket wins.
BACKORDER_THRESHOLDS: list[tuple[int, str]] = [
    (10_000, "📥📥📥 BACKORDER MASSIVE"),
    (5_000, "📥📥 BACKORDER CRITICAL"),
    (1_000, "📥 BACKORDER ALARM"),
    (100, "📥 BACKORDER NOTICE"),
]

# Interleaved severity rank used for header label + sort. Lower = worse.
# OVERSOLD pins to top. BACKORDER MASSIVE slots between OVERSOLD and
# CRITICAL because a 10K+ demand hole is operationally worse than any
# positive-available bucket. Lower backorder buckets slot below the
# matching-severity availability bucket so availability still leads when
# both fire at similar severity.
SEVERITY_RANK: dict[tuple[str, int], int] = {
    ("avail", OVERSOLD_TIER): 0,
    ("avail", 100): 100,
    ("backorder", 10_000): 150,
    ("avail", 500): 200,
    ("backorder", 5_000): 250,
    ("avail", 750): 300,
    ("backorder", 1_000): 350,
    ("avail", 1_000): 400,
    ("backorder", 100): 450,
}

# Velocity sourcing knobs. Per-SKU max page cap keeps total run time bounded;
# effective-window scaling in fetch_sku_depletion handles the saturation case.
VELOCITY_WINDOW_DAYS = 7
VELOCITY_MAX_PAGES = 5

# Amazon US marketplace ID; only marketplace currently in scope for FBA visibility.
US_MARKETPLACE_ID = "ATVPDKIKX0DER"


@dataclass
class Alert:
    label: str
    tier: int  # Interleaved severity rank from SEVERITY_RANK; lower = worse.
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
    # Velocity interpretation context (so readers know what 1,667/day means).
    # depletion_units = total units captured in the sample (saturates at 500).
    # effective_window_days = actual span the captured events covered. When
    # this is much smaller than VELOCITY_WINDOW_DAYS (7), the velocity is an
    # in-stock burst rate, NOT a 7-day average.
    depletion_units: int = 0
    effective_window_days: float = 0.0
    fba_quantity: int | None = None  # Amazon FBA quantity (US marketplace) if present
    # Dual-ladder breakout. `available` is the on_hand-minus-allocated count
    # that drives the availability ladder; `backorder` is the queued-demand
    # count that drives the backorder ladder. `backorder_label` is set when
    # the backorder ladder also triggered (independent of which ladder owns
    # the primary `label`); rendered as a secondary line in the message.
    available: int = 0
    backorder: int = 0
    backorder_label: str | None = None


def _tier_for(value: int) -> tuple[int, str] | None:
    """Single-int availability tier lookup. Negative → OVERSOLD; else ladder
    by upper-bound threshold; None if above the top bucket (healthy).

    Used by `_availability_tier` (which decides whether to pass `on_hand`
    or `available`) and exercised directly in tests.
    """
    if value < 0:
        return OVERSOLD_TIER, OVERSOLD_LABEL
    for threshold, label in THRESHOLDS:
        if value <= threshold:
            return threshold, label
    return None


def _availability_tier(on_hand: int, available: int) -> tuple[int, str] | None:
    """Availability ladder picker.

    OVERSOLD fires on physical-negative on_hand (we owe customers units we
    don't physically have, the most acute operational state). For
    on_hand >= 0, the ladder runs against `available` (sellable units after
    existing-order allocation), so a SKU whose stock is 100% promised to
    queued orders is correctly seen as CRITICAL even when the warehouse
    physically holds units.
    """
    if on_hand < 0:
        return OVERSOLD_TIER, OVERSOLD_LABEL
    return _tier_for(available)


def _backorder_tier_for(backorder: int) -> tuple[int, str] | None:
    """Backorder ladder picker. Walks descending so the worst applicable
    bucket wins. Returns None for backorder < 100 (below noise floor).

    Callers MUST also pass the result through `_backorder_is_alertable`
    against the current `available` count; see that function for why.
    """
    if backorder < BACKORDER_THRESHOLDS[-1][0]:
        return None
    for threshold, label in BACKORDER_THRESHOLDS:
        if backorder >= threshold:
            return threshold, label
    return None


def _backorder_is_alertable(available: int, backorder: int) -> bool:
    """Operational gate for the backorder ladder.

    ShipHero's `warehouse_products.backorder` field is a sticky counter
    rather than a live "currently uncovered demand" number. When stock is
    low it correctly counts units ordered but not in stock; but when
    restock arrives, ShipHero silently re-allocates those backorder
    line_items to fresh stock WITHOUT decrementing the warehouse_products
    backorder counter. Observed 2026-05-20 on BB-CRS-SINGLE (Curl Refresh
    Spray): on_hand=109,038, available=107,856, backorder=996, yet a scan
    of the 25 most recent CRS-containing orders since 2026-04-01 showed
    sum(line_items.backorder_quantity)=0. The 996 is residue from a
    prior stockout, not a current operational gap.

    Gate rule: only fire when current `available` cannot cover the
    backorder counter, i.e. there's a genuine "we owe more than we can
    ship right now" situation. Examples:
    - CLAY1 (available=0, backorder=14,858): 0 <= 14,858 -> fires (real).
    - CRS    (available=107,819, backorder=996): 107,819 > 996 -> suppressed (stale counter).
    """
    return available <= backorder


def _severity_rank(avail_tier: int | None, backorder_tier: int | None) -> tuple[int, str]:
    """Pick the worst interleaved rank across both ladders and return
    (rank, primary_label). primary_label is the header label; the
    secondary ladder (if any) gets rendered separately by build_blocks
    via Alert.backorder_label, not threaded through here.
    """
    candidates: list[tuple[int, str]] = []
    if avail_tier is not None:
        candidates.append(
            (SEVERITY_RANK[("avail", avail_tier)], _label_for_tier("avail", avail_tier))
        )
    if backorder_tier is not None:
        candidates.append(
            (
                SEVERITY_RANK[("backorder", backorder_tier)],
                _label_for_tier("backorder", backorder_tier),
            )
        )
    candidates.sort(key=lambda c: c[0])
    return candidates[0]


def _label_for_tier(kind: str, tier: int) -> str:
    if kind == "avail":
        if tier == OVERSOLD_TIER:
            return OVERSOLD_LABEL
        return next(label for threshold, label in THRESHOLDS if threshold == tier)
    return next(label for threshold, label in BACKORDER_THRESHOLDS if threshold == tier)


def _format_cover(weeks: float) -> str:
    """Render weeks_of_cover for the alert annotation. Sub-week values
    show 2 decimals so 0.4w doesn't get rounded to 0.0w."""
    if weeks >= 9999:
        return "∞ (no observed depletion)"
    if weeks < 1:
        return f"{weeks:.2f}w"
    return f"{weeks:.1f}w"


def _format_sample_window(days: float) -> str:
    """Render the velocity sample window in human terms."""
    if days >= 1.0:
        return f"{days:.1f}d"
    hours = days * 24
    if hours >= 1.0:
        return f"{hours:.1f}h"
    return f"{hours * 60:.0f}min"


def _velocity_interpretation(
    depletion_units: int,
    effective_window_days: float,
    requested_window_days: int,
) -> str | None:
    """Return a one-line annotation showing the sample size + window behind the
    in-stock rate.

    Operational consumers (Avi, Carlos) care about the rate when product is
    actively selling, NOT a calendar average diluted by OOS days — those just
    become backorders. So we surface what's load-bearing for planning: how
    many units depleted, over how much in-stock activity the rate was
    sampled. The calendar-avg framing was actively misleading and was
    removed 2026-05-08 after operational feedback.

    Known limitation: the rate does NOT account for channel-availability
    state. When ops marks a SKU OOS on Shopify / TTS / Amazon, demand stops
    registering on that channel even though customer interest may persist.
    Bot has no view into those flags, so the in-stock rate it reports is a
    "rate during periods where SOME channel was selling," not steady-state
    demand. Operator judgment (knowing which channels were live) outranks
    this number when they disagree. See docs/plans/2026-05-08-channel-state-
    limitations.md if/when we add OOS-flag tracking.
    """
    if effective_window_days <= 0:
        return None
    # If we got the full requested window, velocity is unambiguous.
    if effective_window_days >= requested_window_days * 0.95:
        return None
    sample = _format_sample_window(effective_window_days)
    return (
        f"📊  {depletion_units:,} units shipped last {requested_window_days}d. "
        f"In-stock rate sampled over {sample} of activity."
    )


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


def build_blocks(
    alerts: list[Alert], channel_mix_summary: str | None = None
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        header("⚡ Inventory Alert"),
        divider(),
    ]

    for a in alerts:
        text_lines = [
            f"{a.label}  •  *{a.product_name or a.sku}*",
        ]
        if a.backorder_label and not a.label.startswith("📥"):
            text_lines.append(a.backorder_label)
        stock_parts = [f"*{a.on_hand:,}* on hand"]
        if a.available != a.on_hand:
            stock_parts.append(f"*{a.available:,}* available")
        if a.backorder > 0:
            stock_parts.append(f"*{a.backorder:,}* backordered")
        owe_suffix = " (already owe customers units)" if a.on_hand < 0 else ""
        text_lines.append("📦  " + "  •  ".join(stock_parts) + owe_suffix)
        if a.velocity_per_day > 0:
            # Label the velocity correctly: if the sample window is much
            # smaller than requested, this is in-stock burst rate, not avg.
            is_burst = (
                a.effective_window_days > 0
                and a.effective_window_days < VELOCITY_WINDOW_DAYS * 0.95
            )
            label = "in-stock rate" if is_burst else "velocity"
            text_lines.append(
                f"⏱️  {_format_cover(a.weeks_of_cover)} cover at {a.velocity_per_day:,.0f}/day {label}"
            )
            interp = _velocity_interpretation(
                a.depletion_units, a.effective_window_days, VELOCITY_WINDOW_DAYS
            )
            if interp:
                text_lines.append(interp)
        elif a.on_hand >= 0:
            text_lines.append("⏱️  no recent depletion observed")
        if a.fba_quantity is not None:
            text_lines.append(f"🅰️   Amazon FBA on-hand: *{a.fba_quantity:,}* units (US)")
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
        s
        for s in stock
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
        inbound = client.fetch_inbound_outstanding_by_sku(po_date_from_iso="2025-01-01T00:00:00")
    except RuntimeError:
        inbound = {}

    alerts: list[Alert] = []
    new_avail_tiers: dict[str, int] = {}
    new_backorder_tiers: dict[str, int] = {}
    # FBA quantity is queried lazily per SKU at alert time (cheap; only fires
    # for at-risk SKUs, not every candidate). Only US marketplace is summed.
    for s in candidates:
        avail_info = _availability_tier(s.on_hand, s.available)
        backorder_info = _backorder_tier_for(s.backorder)
        if backorder_info is not None and not _backorder_is_alertable(s.available, s.backorder):
            # ShipHero's backorder counter is sticky after restock; suppress
            # the ladder when current available stock can cover the queued
            # backorder. See _backorder_is_alertable docstring for evidence.
            backorder_info = None

        # Always record current ladder positions for the next-run dedup, even
        # for SKUs that don't fire this run (otherwise a SKU sitting at the
        # same tier for weeks would re-fire on every backorder fluctuation).
        if avail_info is not None:
            new_avail_tiers[s.sku] = avail_info[0]
        else:
            state.clear_tier(s.sku)
        if backorder_info is not None:
            new_backorder_tiers[s.sku] = backorder_info[0]
        else:
            state.clear_backorder_tier(s.sku)

        # Dedup: fire if EITHER ladder crossed to a worse bucket since the
        # prior run. Quiet recovery (improving tiers) is suppressed by both
        # cross checks returning False.
        avail_crossed = avail_info is not None and state.crosses_lower_tier(s.sku, avail_info[0])
        backorder_crossed = backorder_info is not None and state.crosses_higher_backorder_tier(
            s.sku, backorder_info[0]
        )
        if not (avail_crossed or backorder_crossed):
            continue

        rank, primary_label = _severity_rank(
            avail_info[0] if avail_info else None,
            backorder_info[0] if backorder_info else None,
        )

        cover = sku_cover.get(s.sku)
        inb = inbound.get(s.sku) or {}

        # Pull FBA quantity for the at-risk SKU. Only ~10 SKUs cross thresholds
        # per run so the credit cost is bounded. Best-effort: if it errors or
        # the SKU has no FBA record, fba_quantity stays None.
        fba_qty: int | None = None
        try:
            fba_rows = client.fetch_fba_inventory(s.sku)
            us_rows = [r for r in fba_rows if r.get("marketplace_id") == US_MARKETPLACE_ID]
            if us_rows:
                fba_qty = sum(int(r.get("quantity") or 0) for r in us_rows)
        except RuntimeError:
            pass

        alerts.append(
            Alert(
                label=primary_label,
                tier=rank,
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
                depletion_units=depletion.get(s.sku, 0),
                effective_window_days=eff_windows.get(s.sku, 0.0),
                fba_quantity=fba_qty,
                available=s.available,
                backorder=s.backorder,
                backorder_label=backorder_info[1] if backorder_info else None,
            )
        )

    state.quantity_tiers = new_avail_tiers
    state.backorder_tiers = new_backorder_tiers
    state.save(cfg.state_path)

    if not alerts:
        return

    # Sort by interleaved severity rank ASC (0 = OVERSOLD, 100 = CRITICAL,
    # 150 = BACKORDER MASSIVE, ..., 450 = BACKORDER NOTICE), then by on_hand
    # ASC as tiebreaker so the most-depleted SKU surfaces first within a rank.
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
