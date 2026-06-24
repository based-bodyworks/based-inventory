"""Inventory math: weeks_of_cover per SKU and per kit/bundle.

v0 keeps the math simple (per the design): no campaign-lift simulation,
on-hand-only (sell_ahead is informational, not added). Velocity comes from
inventory_changes over a configurable lookback window, defaulting to 30 days.

Tier thresholds (defaults, tunable via env):
- at_risk:  weeks_of_cover < 2
- healthy:  2 <= weeks_of_cover < 8
- surplus:  weeks_of_cover >= 8

CRITICAL DATA-TRUST RULE
------------------------
Per Carlos / David (2026-04-27): the on_hand / available / velocity numbers
ShipHero reports for KIT SKUs are fabricated and meaningless. The only
trustworthy inventory numbers are component (single, non-kit) SKU counts.

Therefore, for any KIT SKU (where Product.kit == true / WarehouseStock.is_kit
== true), we MUST NOT display or rank by its raw on_hand, available, or
velocity. Bundle-level cover is always derived from component cover via
min-of-components in compute_bundle_cover(). The only surfaces that ever
expose kit SKU numerics directly are the bundle representative_name and
the cluster member_skus list (identity, not quantity).

If you are reading this and considering adding a "show kit on_hand" field
or a "kit velocity" tile, STOP. The data is fabricated. There are tests
in test_inventory.py that will fail if you wire kit numerics through.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .registry import BundleEntry, BundleRegistry
from .shiphero import KitDefinition, WarehouseStock

DEFAULT_VELOCITY_WINDOW_DAYS = 30
TIER_AT_RISK_BELOW = 2.0
TIER_SURPLUS_AT_OR_ABOVE = 8.0
INFINITE_COVER = 10_000.0  # sentinel for SKUs with no observed depletion


@dataclass(frozen=True)
class SkuCover:
    sku: str
    name: str
    on_hand: int
    available: int
    velocity_per_day: float
    weeks_of_cover: float
    tier: str
    is_kit: bool  # ShipHero kit=true flag.
    trusted_inventory: bool  # False if this SKU is in the BundleRegistry; on_hand is fabricated.


@dataclass(frozen=True)
class BundleCover:
    bundle_sku: str
    bundle_name: str
    source: str  # "shiphero" or "set-components"
    component_skus: tuple[str | None, ...]  # None for unresolved set-components names
    weeks_of_cover: float
    tier: str
    bottleneck_sku: str | None
    partially_resolved: bool


def _tier(weeks: float) -> str:
    if weeks < TIER_AT_RISK_BELOW:
        return "at_risk"
    if weeks >= TIER_SURPLUS_AT_OR_ABOVE:
        return "surplus"
    return "healthy"


def aggregate_velocity_from_orders(
    orders: list[dict],
    registry: BundleRegistry,
) -> dict[str, int]:
    """Walk every order's line_items and sum `quantity` per component SKU.

    When a line_item SKU is itself a bundle (in registry.bundle_skus), expand
    it to component SKUs via the BundleRegistry, multiplying by the kit's
    component quantities. The point: velocity for "Shampoo" should include
    every Shower Duo and Curly Bundle that contained shampoo.

    Returns: SKU -> total units depleted across all orders.
    """
    by_bundle = registry.by_bundle_sku
    out: dict[str, int] = {}
    for order in orders:
        for li in (order.get("line_items") or {}).get("edges", []):
            n = li.get("node") or {}
            sku = n.get("sku")
            qty = int(n.get("quantity") or 0)
            if not sku or qty <= 0:
                continue
            entry = by_bundle.get(sku)
            if entry is None:
                # Real single (or unknown SKU); count its own depletion
                out[sku] = out.get(sku, 0) + qty
                continue
            # It's a bundle SKU; expand to components
            for comp_sku, _name, comp_qty in entry.components_resolved:
                if comp_sku is None:
                    continue
                out[comp_sku] = out.get(comp_sku, 0) + qty * comp_qty
    return out


def compute_sku_cover(
    stock: Iterable[WarehouseStock],
    depletion_by_sku: dict[str, int],
    window_days: int = DEFAULT_VELOCITY_WINDOW_DAYS,
    registry: BundleRegistry | None = None,
    effective_window_by_sku: dict[str, float] | None = None,
) -> dict[str, SkuCover]:
    """Build SkuCover per warehouse_product row.

    Args:
        stock: rows from ShipHeroClient.fetch_warehouse_stock.
        depletion_by_sku: SKU -> total units depleted in the window.
        window_days: requested window in days (used as fallback divisor).
        registry: BundleRegistry. SKUs in registry.bundle_skus get
            trusted_inventory=False.
        effective_window_by_sku: optional SKU -> actual time span (in days)
            of captured depletion events. When the page cap saturated for
            a high-velocity SKU, this is shorter than `window_days` and
            MUST be used as the divisor to avoid undercounting velocity.

    Returns: SKU -> SkuCover.
    """
    out: dict[str, SkuCover] = {}
    bundle_skus = registry.bundle_skus if registry is not None else frozenset()
    eff_window = effective_window_by_sku or {}
    for s in stock:
        depleted = depletion_by_sku.get(s.sku, 0)
        sku_window = max(eff_window.get(s.sku, float(window_days)), 1 / 24.0)
        velocity = depleted / sku_window if sku_window > 0 else 0.0
        weeks = INFINITE_COVER if velocity <= 0 else s.on_hand / (velocity * 7)
        is_known_bundle = s.sku in bundle_skus or s.is_kit
        out[s.sku] = SkuCover(
            sku=s.sku,
            name=s.product_name,
            on_hand=s.on_hand,
            available=s.available,
            velocity_per_day=velocity,
            weeks_of_cover=weeks,
            tier=_tier(weeks),
            is_kit=s.is_kit,
            trusted_inventory=not is_known_bundle,
        )
    return out


def compute_bundle_cover(
    registry: BundleRegistry,
    sku_cover: dict[str, SkuCover],
) -> list[BundleCover]:
    """Roll up bundle-level cover from the merged BundleRegistry.

    For each BundleEntry, weeks_of_cover is `min(weeks_of_cover[component])`
    over its components, treating each component's quantity multiplicatively
    against velocity (a Pack of 3 needs 3x the units per kit sold, so its
    effective cover halves to a third of the single-unit cover).

    A component is treated as UNKNOWN (cover = 0, conservative) when:
    - It does not appear in sku_cover (no inventory data fetched).
    - It has trusted_inventory=False (it's itself a bundle whose on_hand
      is fabricated).
    - It was unresolved during registry build (set-components.json name
      did not match any SKU).

    The bundle's bottleneck_sku is the component pinning the minimum.
    """
    bundles: list[BundleCover] = []
    for entry in registry.bundles:
        per_component_weeks: list[tuple[str | None, float]] = []
        for comp_sku, _comp_name, qty in entry.components_resolved:
            if comp_sku is None:
                per_component_weeks.append((comp_sku, 0.0))
                continue
            comp = sku_cover.get(comp_sku)
            if comp is None or not comp.trusted_inventory:
                per_component_weeks.append((comp_sku, 0.0))
                continue
            if qty > 1 and comp.velocity_per_day > 0:
                effective_weeks = comp.on_hand / (comp.velocity_per_day * qty * 7)
            else:
                effective_weeks = comp.weeks_of_cover
            per_component_weeks.append((comp_sku, effective_weeks))

        if not per_component_weeks:
            continue
        bottleneck_sku, weeks = min(per_component_weeks, key=lambda x: x[1])
        bundles.append(
            BundleCover(
                bundle_sku=entry.bundle_sku,
                bundle_name=entry.bundle_name,
                source=entry.source,
                component_skus=tuple(c[0] for c in entry.components_resolved),
                weeks_of_cover=weeks,
                tier=_tier(weeks),
                bottleneck_sku=bottleneck_sku,
                partially_resolved=entry.partially_resolved,
            )
        )
    return bundles


# Backwards-compatibility shim: older callers pass kit_clusters, but the new
# pipeline drives bundle math from the merged BundleRegistry. Keep this
# helper out of the public API.
def _bundle_entries_from_kits(kits: list[KitDefinition]) -> list[BundleEntry]:
    return [
        BundleEntry(
            bundle_sku=k.sku,
            bundle_name=k.name,
            source="shiphero",
            components_resolved=tuple((sku, sku, qty) for sku, qty in k.components),
            partially_resolved=False,
        )
        for k in kits
    ]


@dataclass(frozen=True)
class DemandSample:
    """48h order-intake demand for one component SKU.

    units: total units ordered across the window, with bundles exploded into
        component quantities (a Curly Kit order adds to Curl Cream).
    order_count: number of DISTINCT orders that contained this component
        (directly or via any bundle). An order that holds the component both
        as a single and inside a kit counts once.
    """

    units: int
    order_count: int


def aggregate_orders_demand(
    orders: list[dict],
    registry: BundleRegistry,
) -> dict[str, DemandSample]:
    """Aggregate order-intake demand per component SKU over a set of orders.

    ShipHero order line_items contain BOTH the kit SKU (a logical wrapper
    line) AND the kit's components pre-exploded as separate lines, at
    quantities matching the kit definition (verified empirically on
    2026-06-10: 5,695/5,696 kit-orders matched exactly). So per order:

      - A kit line whose components are already pre-listed at >= definition
        quantities is a MARKER: the component lines carry the demand and the
        kit line adds nothing. Exploding it on top would double-count.
      - A kit line whose components are NOT pre-listed (e.g. some website
        set SKUs) is exploded into components via the BundleRegistry.

    Mirrors order_physical_contents in based-packaging-optimization/
    scripts/analyze_order_profiles.py (the verified reference): larger kits
    are checked first so a duo doesn't claim a deluxe kit's pre-listed
    components.

    For each component SKU we track two things:
      - units: total deduped units across all orders.
      - order_count: distinct orders that touched this component. A single
        order is counted once per component even if the component appears both
        as a standalone line and inside a bundle line in that same order.

    Returns: SKU -> DemandSample.
    """
    by_bundle = registry.by_bundle_sku
    units: dict[str, int] = {}
    order_count: dict[str, int] = {}
    for order in orders:
        # Pass 1: collapse this order's lines into sku -> qty.
        lines: dict[str, int] = {}
        for li in (order.get("line_items") or {}).get("edges", []):
            n = li.get("node") or {}
            sku = n.get("sku")
            qty = int(n.get("quantity") or 0)
            if not sku or qty <= 0:
                continue
            lines[sku] = lines.get(sku, 0) + qty

        # Pass 2: non-kit lines are physical demand as-is; kit lines are
        # markers when pre-exploded, definitions to add when not.
        physical: dict[str, int] = {s: q for s, q in lines.items() if s not in by_bundle}
        kit_lines = [(s, q) for s, q in lines.items() if s in by_bundle]
        kit_lines.sort(key=lambda kq: -len(by_bundle[kq[0]].components_resolved))
        for kit_sku, kit_qty in kit_lines:
            needed: dict[str, int] = {}
            for comp_sku, _name, comp_qty in by_bundle[kit_sku].components_resolved:
                if comp_sku is None:
                    continue
                needed[comp_sku] = needed.get(comp_sku, 0) + comp_qty * kit_qty
            if needed and all(physical.get(c, 0) >= n for c, n in needed.items()):
                continue  # pre-exploded: component lines already carry it
            for comp_sku, n in needed.items():
                physical[comp_sku] = physical.get(comp_sku, 0) + n

        for sku, qty in physical.items():
            units[sku] = units.get(sku, 0) + qty
            order_count[sku] = order_count.get(sku, 0) + 1
    return {
        sku: DemandSample(units=units.get(sku, 0), order_count=order_count.get(sku, 0))
        for sku in units
    }
