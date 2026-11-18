"""A complete physical-SKU daily view built only from verified order days."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import date, timedelta
from html import escape
from pathlib import Path

from based_inventory.pipeline.models import VerifiedDay

DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parent / "pipeline/cloud_helpers/inventory_catalog.json"
)
MAX_SLACK_CHARACTERS = 39_000

_GROUPS = (
    "Hair Care",
    "Straight/Wavy Styling",
    "Curly Styling",
    "Body",
    "Skin",
    "Minis",
    "Accessories",
    "Retail packaging (online orders only)",
    "Other tracked SKUs",
    "Other sold SKUs (mapping unverified)",
)
_HAIR = frozenset({"44114434293989", "44114437669093", "BB-HRELXR-SINGLE", "BB-HRS-01"})
_STRAIGHT = frozenset({"44126606262501", "CLAY1", "BB2000S-1-6-1", "POMADE1"})
_CURLY = frozenset(
    {"BB-LEAVEIN-ONE", "BB-CC-SINGLE", "BB-MOUS-SINGLE", "BB-CGEL-SINGLE", "BB-CRS-SINGLE"}
)


def _group(sku: str) -> str:
    # Retail packaging and minis stay separate even when they share a product family.
    if sku.endswith("-RETAIL"):
        return "Retail packaging (online orders only)"
    if "-MINI" in sku:
        return "Minis"
    if sku in _HAIR:
        return "Hair Care"
    if sku in _STRAIGHT:
        return "Straight/Wavy Styling"
    if sku in _CURLY:
        return "Curly Styling"
    if sku.startswith(("BB-BDYLTN-", "BB-DEO-")) or sku in {
        "BB-SS-01",
        "BB-GN-01",
        "BB-CC-01",
    }:
        return "Body"
    if sku.startswith(("BB-ACCS-", "BB-GWP-")):
        return "Accessories"
    if sku.startswith(("BB-ONE-", "BB-UEE-", "BB-SRS-", "BB-SKNELXR-")):
        return "Skin"
    return "Other tracked SKUs"


def _catalog(path: Path) -> dict[str, str]:
    entries = json.loads(path.read_text())
    if not isinstance(entries, list) or not entries:
        raise ValueError("Physical SKU catalog must be a nonempty list")
    result = {}
    for row in entries:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(k), str) or not row[k].strip() for k in ("sku", "product")
        ):
            raise ValueError("Invalid physical SKU catalog row")
        if row["sku"] in result:
            raise ValueError("Duplicate physical SKU in catalog")
        result[row["sku"]] = row["product"]
    return result


def _units(version: VerifiedDay) -> dict[str, int]:
    units: dict[str, int] = defaultdict(int)
    for row in version.rows:
        units[row.sku] += row.units
    return dict(units)


def _text(value: str) -> str:
    # Names come from provider data. Never turn an unknown product into a Slack mention.
    return escape(" ".join(value.split()), quote=False).replace("`", "'").replace("\u2014", "-")


def render_report(
    version: VerifiedDay,
    history: Mapping[date, VerifiedDay],
    catalog_path: Path | None = None,
) -> str:
    """Show all tracked SKUs; an average requires all seven exact complete UTC days.

    ``history`` contains already verified physical component units, never raw bundle
    listings. Missing history is distinct from an observed zero in a verified day.
    The selected ``version`` always supplies the current reporting day.
    """
    if not isinstance(version, VerifiedDay):
        raise ValueError("Expected a verified reporting day")
    for key, record in history.items():
        if type(key) is not date or not isinstance(record, VerifiedDay) or key != record.day:
            raise ValueError("Velocity history key does not match its verified day")
    names = _catalog(catalog_path or DEFAULT_CATALOG_PATH)
    tracked_count = len(names)
    window = tuple(version.day - timedelta(days=n) for n in reversed(range(7)))
    days = {day: history[day] for day in window if day in history}
    days[version.day] = version
    missing = [day for day in window if day not in days]
    complete = not missing
    day_units = _units(version)
    seven_units: dict[str, int] = defaultdict(int)
    unknown_names = {}
    for day in sorted(days):
        for row in days[day].rows:
            seven_units[row.sku] += row.units
            if row.sku not in names and row.units > 0:
                unknown_names[row.sku] = row.product
    names.update({sku: unknown_names[sku] for sku in sorted(unknown_names)})
    collisions = Counter(name.casefold().strip() for name in names.values())
    total = sum(day_units.values())
    average = f"*{sum(seven_units.values()) / 7:,.1f}/day*" if complete else "n/a"
    extra = len(unknown_names)
    count = f"{tracked_count} tracked SKUs"
    if extra:
        count += f" + {extra} additional sold SKU{'s' if extra != 1 else ''}"
    totals = version.totals
    lines = [
        f"*Daily SKU Velocity | {version.day} (UTC)*",
        "Merchdrop warehouse customer orders; one complete UTC day. "
        "Gross ordered units before cancellation or return adjustments.",
        f"{count}. Each row: *day units* | *7-day average units/day*.",
        f"Total day units: *{total:,}* | 7-day average: {average}",
        f"Day channels: Shopify {totals['Shopify']:,} · TikTok {totals['TIKTOK']:,} · "
        f"Amazon merchant-fulfilled {totals['Amazon FBM']:,}",
        f"7-day window: {window[0]} through {window[-1]} (UTC); " f"{len(days)}/7 verified days.",
    ]
    if missing:
        lines.append("7-day averages unavailable. Missing: " + ", ".join(map(str, missing)) + ".")
    sections: dict[str, list[str]] = defaultdict(list)
    for sku, name in names.items():
        label = _text(name)
        if sku in unknown_names or collisions[name.casefold().strip()] > 1:
            label += f" (`{_text(sku)}`)"
        rate = f"{seven_units[sku] / 7:,.1f}/day" if complete else "n/a"
        category = "Other sold SKUs (mapping unverified)" if sku in unknown_names else _group(sku)
        sections[category].append(f"• {label}: *{day_units.get(sku, 0):,}* | {rate}")
    for category in _GROUPS:
        if sections[category]:
            lines.extend(("", f"*{category}*", *sections[category]))
    lines.extend(
        (
            "",
            "Units ordered, including gifts; packs and bundles are already counted "
            "in their component SKUs. This is observed order demand, not shipments or a forecast.",
            "Stockouts can limit observed orders.",
            "Amazon-held fulfillment sales, Target store sales and Target replenishment "
            "shipments are excluded. Retail packaging rows cover only the online order channels above.",
            f"Source: ShipHero. Checked {version.coverage.fetched_unique:,} warehouse orders "
            "with complete order and item coverage. Channel detail is in the workbook.",
        )
    )
    if unknown_names:
        lines.append(
            "Unverified product mappings remain in the totals; review the Other sold SKUs rows."
        )
    result = "\n".join(lines)
    if len(result) > MAX_SLACK_CHARACTERS:
        raise ValueError("Complete SKU report exceeds the Slack message limit; no rows were sent")
    return result
