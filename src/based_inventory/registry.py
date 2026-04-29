"""Bundle registry: merges ShipHero kit_components and set-components.json.

CRITICAL DATA-TRUST RULE
------------------------
Per Carlos / David (2026-04-27): ShipHero's on_hand / available / velocity
numbers are TRUSTWORTHY ONLY for individual physical SKUs. They are
FABRICATED for any SKU that represents a bundle / kit / multi-pack /
gift set / channel-specific listing.

The bot identifies a SKU as "a bundle" via two registries:
1. **ShipHero kit_components**: SKUs returned by `products(has_kits: true)`.
   These are explicit kits where ShipHero knows the components.
2. **set-components.json**: human-readable Shopify-website bundle names
   mapped to component-name lists. These are bundles whose ShipHero SKU
   may NOT be flagged as a kit (kit=false) but are still bundles whose
   on_hand cannot be trusted.

A SKU is TRUSTED iff it is NOT in the union of (1) and (2). Tied
together via product names from warehouse_products (set-components.json
is name-keyed; ShipHero is SKU-keyed; we resolve via WarehouseStock.name).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .shiphero import KitDefinition, WarehouseStock


@dataclass(frozen=True)
class BundleEntry:
    """A normalized bundle definition, regardless of source.

    components_resolved is the list of (component_sku, quantity) pairs
    AFTER name-to-SKU resolution. If a name in set-components.json could
    not be matched to any warehouse_product, that component appears as
    None and the bundle is marked `partially_resolved=True`.
    """
    bundle_sku: str
    bundle_name: str
    source: str  # "shiphero" or "set-components"
    components_resolved: tuple[tuple[str | None, str, int], ...]
    # Each tuple: (component_sku_or_none, component_name, quantity)
    partially_resolved: bool


@dataclass(frozen=True)
class BundleRegistry:
    """Indexed view over all known bundles + which SKUs to distrust."""
    bundles: tuple[BundleEntry, ...]
    bundle_skus: frozenset[str]
    # Convenience: SKU -> BundleEntry (for the most-specific entry if multiple sources match)
    by_bundle_sku: dict[str, BundleEntry]

    def is_trusted_single(self, sku: str) -> bool:
        return sku not in self.bundle_skus


def _load_set_components(path: Path) -> dict[str, list[str]]:
    raw = json.loads(path.read_text())
    return raw.get("sets", {})


def _index_stock_by_name(stock: list[WarehouseStock]) -> dict[str, list[WarehouseStock]]:
    """Map exact product_name -> list of WarehouseStock entries.

    A product name may appear on multiple SKUs (e.g., the human-readable
    "Shower Duo" might exist as a non-kit SKU for the Shopify storefront
    AND as a kit SKU). We return the list and let the caller pick.
    """
    out: dict[str, list[WarehouseStock]] = {}
    for s in stock:
        out.setdefault(s.product_name.strip(), []).append(s)
    return out


def _name_match(name: str, by_name: dict[str, list[WarehouseStock]]) -> WarehouseStock | None:
    """Look up a single SKU for a component or bundle name.

    Strategy:
    1. Exact match.
    2. Case-insensitive exact.
    3. Substring match (the search name appears within a longer ShipHero
       name like "BASED Curly Duo | Leave-In Conditioner & Curl Cream"
       — empirically observed for 9/24 set-components.json bundles).

    When multiple SKUs match, prefer the one with the highest on_hand
    (assumed to be the canonical physical SKU rather than a zeroed-out
    legacy variant or duplicate channel listing).
    """
    target = name.strip()
    target_l = target.lower()

    candidates = by_name.get(target)
    if candidates:
        return max(candidates, key=lambda s: s.on_hand)

    for k, v in by_name.items():
        if k.lower() == target_l:
            return max(v, key=lambda s: s.on_hand)

    # Substring fallback: search name appears within a ShipHero product name.
    # Guard against trivial matches by requiring the target to be at least
    # 4 chars and to appear as a whole-word boundary.
    if len(target_l) < 4:
        return None
    substring_candidates: list[WarehouseStock] = []
    for k, v in by_name.items():
        kl = k.lower()
        if target_l in kl:
            # Whole-word boundary check: the match must be flanked by a
            # non-letter character or end-of-string.
            idx = kl.find(target_l)
            before_ok = idx == 0 or not kl[idx - 1].isalnum()
            after_ok = (idx + len(target_l) == len(kl)) or not kl[idx + len(target_l)].isalnum()
            if before_ok and after_ok:
                substring_candidates.extend(v)
    if substring_candidates:
        return max(substring_candidates, key=lambda s: s.on_hand)
    return None


def build_registry(
    kits: list[KitDefinition],
    stock: list[WarehouseStock],
    set_components_path: Path | str,
) -> BundleRegistry:
    """Merge ShipHero kit_components and set-components.json.

    Resulting BundleRegistry is the source of truth for "which SKUs are
    bundles" and therefore "which SKUs have untrustworthy on_hand."
    """
    by_name = _index_stock_by_name(stock)
    bundles: list[BundleEntry] = []
    bundle_skus: set[str] = set()

    # 1. ShipHero kits (SKU-keyed)
    for kit in kits:
        bundle_skus.add(kit.sku)
        components_resolved = tuple(
            (sku, sku, qty) for sku, qty in kit.components  # ShipHero already gives us SKUs
        )
        bundles.append(
            BundleEntry(
                bundle_sku=kit.sku,
                bundle_name=kit.name,
                source="shiphero",
                components_resolved=components_resolved,
                partially_resolved=False,
            )
        )

    # 2. set-components.json (name-keyed) — resolve names to SKUs.
    sets = _load_set_components(Path(set_components_path))
    for bundle_name, component_names in sets.items():
        bundle_match = _name_match(bundle_name, by_name)
        if bundle_match is None:
            # Bundle name not present in this warehouse's stock; skip.
            # (It may live only at the Wilshire warehouse, or be retired.)
            continue
        bundle_sku = bundle_match.sku
        if bundle_sku in bundle_skus:
            # Already covered by ShipHero kit registry; ShipHero is more
            # canonical. Skip.
            continue
        resolved: list[tuple[str | None, str, int]] = []
        partial = False
        for cname in component_names:
            comp_match = _name_match(cname, by_name)
            if comp_match is None:
                resolved.append((None, cname, 1))
                partial = True
            else:
                resolved.append((comp_match.sku, cname, 1))
        bundle_skus.add(bundle_sku)
        bundles.append(
            BundleEntry(
                bundle_sku=bundle_sku,
                bundle_name=bundle_name,
                source="set-components",
                components_resolved=tuple(resolved),
                partially_resolved=partial,
            )
        )

    by_bundle_sku = {b.bundle_sku: b for b in bundles}
    return BundleRegistry(
        bundles=tuple(bundles),
        bundle_skus=frozenset(bundle_skus),
        by_bundle_sku=by_bundle_sku,
    )
