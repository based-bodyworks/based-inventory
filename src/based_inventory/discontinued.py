"""Live-SKU filter for ShipHero-sourced alerts.

The bot must skip three classes of SKUs before alerting on quantity tiers
or oversold inventory:

1. Test / cruft / legacy SKUs that exist in ShipHero but aren't real
   live products. Filtered by heuristic name patterns.
2. SKUs Avi has explicitly marked as end-of-life / not-restocking. Lives
   in `data/discontinued-skus.json` and is editable by Avi.
3. Channel / warehouse variant suffixes (-FBA, -CASE, -PALLET) that
   represent the same physical product as another SKU; alerting on
   them duplicates the underlying SKU's signal.

This is the ShipHero analog of the legacy `skip_list.py` (which was
keyed by Shopify product title). The two coexist; both are applied.
"""

from __future__ import annotations

import json
from pathlib import Path

# Heuristic patterns. All lowercase comparisons; pattern is checked against
# the product name padded with spaces ` <name> ` so " 1.0 " matches at word
# boundaries. Parenthesized markers strip internal whitespace before matching
# so "(Teasers )" and "(Teasers)" both match "(teasers)".
_TEST_PATTERNS = (
    # Test / placeholder markers
    "(test)",
    "(testing)",
    "(don't use)",
    "(dont use)",
    "(don't touch)",
    "(dont touch)",
    "(copy)",
    "(teasers)",
    "(review)",
    "(yassir)",
    "(bfcm)",  # Black Friday / Cyber Monday seasonal SKUs
    # Legacy version markers
    " 1.0 ",
    " 2.0 ",
    # Cruft / non-physical / one-off
    "troubleshoot",
    "showerhead filter",
    "stone bath mat",
    "shipping protection",
    "brand ambassador",
    # Retired product lines (legacy skip_list.py entries)
    "based membership",
    "based shampoo 1.0",
    "based shampoo 2.0",
    "based conditioner 1.0",
    "based conditioner 2.0",
    "hair revival serum",
    "scalp revival serum",
    "skin revival serum",
    "revival serums",
    "super serum",
    "whipped tallow moisturizer",
    "based wooden comb - light",
    "bath stone (white)",
    "shampoo + conditioner bundle sample",
    "4oz shampoo + conditioner bundle sample",
    # Fulfillment / shipping placeholder SKUs
    "shipping",
    "shipping international",
    # ShipHero zombie / unnamed
    "default",
    "bundlesuite",
    "untitled",
)

# SKU suffixes that indicate a channel/warehouse variant of an underlying
# physical SKU. Skip these to avoid duplicate alerts. Match is case-insensitive.
_SKU_SUFFIXES = (
    "-FBA",
    "-FBA - FN",
    "-FBA - FNSKU",
    "-CASE",
    "-PALLET",
    " - FBA",
    " - FBA - FN",
    " - FBA - FNSKU",
)


def _heuristic_skip(product_name: str, sku: str) -> bool:
    name = (product_name or "").lower().strip()
    sku_str = (sku or "").strip()
    sku_lower = sku_str.lower()
    if not name and not sku_str:
        return False
    # Normalize whitespace inside parens: "(teasers )" -> "(teasers)".
    name_normalized = name
    while " )" in name_normalized:
        name_normalized = name_normalized.replace(" )", ")")
    while "( " in name_normalized:
        name_normalized = name_normalized.replace("( ", "(")
    # Pad with spaces so " 1.0 " / " 2.0 " match at word boundaries.
    padded = f" {name_normalized} "
    for pat in _TEST_PATTERNS:
        if pat in padded:
            return True
    return any(
        sku_lower.endswith(suffix.lower()) for suffix in _SKU_SUFFIXES
    )


def _load_discontinued(path: Path) -> dict[str, dict[str, str]]:
    """Read the manual discontinued list. Returns SKU -> entry dict.

    Missing file is acceptable (returns empty dict) so the bot can run
    before Avi has populated it.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    skus = raw.get("skus") or []
    out: dict[str, dict[str, str]] = {}
    for entry in skus:
        if isinstance(entry, dict) and entry.get("sku"):
            out[str(entry["sku"])] = entry
    return out


class DiscontinuedFilter:
    """Combined heuristic + manual SKU filter.

    Build once per cron run (cheap; one JSON read). Call `should_skip(sku, name)`
    for every WarehouseStock row before applying tier logic.
    """

    def __init__(self, discontinued_path: Path | str) -> None:
        self._manual = _load_discontinued(Path(discontinued_path))

    def should_skip(self, sku: str, product_name: str) -> bool:
        if sku in self._manual:
            return True
        return _heuristic_skip(product_name, sku)

    def manual_entries(self) -> dict[str, dict[str, str]]:
        return dict(self._manual)
