"""Reader-facing all-channel sales counts with a separate warehouse reference."""

from __future__ import annotations

import io
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from based_inventory.daily_velocity_report import (
    _GROUPS,
    DEFAULT_CATALOG_PATH,
    MAX_SLACK_CHARACTERS,
    _catalog,
    _group,
    _text,
)
from based_inventory.pipeline.models import VerifiedDay

if TYPE_CHECKING:
    from based_inventory.all_channel_sales import SalesDay

CHANNELS = ("Shopify", "TikTok", "Amazon", "Target")
_RETAIL = "Retail packaging"
_UNKNOWN = "Other sold SKUs (mapping unverified)"
_RECIPE_HISTORY = (
    "Packs use recipes available at collection time; past recipe changes are not reconstructed."
)
_SECTION_ORDER = tuple(
    _RETAIL if group.startswith("Retail packaging") else group for group in _GROUPS
)


@dataclass(frozen=True)
class _View:
    names: dict[str, str]
    categories: dict[str, str]
    tracked_count: int
    unknown: frozenset[str]
    current_unverified: bool
    unverified_channels: frozenset[str]
    current_units: dict[str, int]
    channel_units: dict[tuple[str, str], int]
    seven_units: dict[str, int]
    window: tuple[date, ...]
    available: tuple[date, ...]
    missing: tuple[date, ...]
    mismatched: tuple[date, ...]
    complete_average: bool


def _clean(value: object) -> str:
    return " ".join(str(value).split()).replace("\u2014", "-")


def _row_units(record) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    for row in record.rows:
        if row.channel not in CHANNELS or type(row.units) is not int or row.units < 0:
            raise ValueError("Invalid all-channel unit row")
        result[row.sku] += row.units
    return dict(result)


def _view(current, history, catalog_path) -> _View:
    if type(current.day) is not date or not isinstance(current.timezone, str):
        raise ValueError("Invalid all-channel reporting date or timezone")
    if set(current.sources) != set(CHANNELS):
        raise ValueError("All four source captures are required")
    for key, record in history.items():
        if type(key) is not date or key != record.day:
            raise ValueError("All-channel history key differs from reporting date")
    names = _catalog(catalog_path or DEFAULT_CATALOG_PATH)
    tracked_count = len(names)
    known = frozenset(names)
    window = tuple(current.day - timedelta(days=n) for n in reversed(range(7)))
    records = {day: history[day] for day in window if day in history}
    records[current.day] = current
    unknown_names = {}
    for day in sorted(records):
        for row in records[day].rows:
            if row.units > 0 and row.sku not in known:
                unknown_names[row.sku] = row.product
    names.update({sku: unknown_names[sku] for sku in sorted(unknown_names)})
    categories = {
        sku: _UNKNOWN
        if sku in unknown_names
        else (_RETAIL if _group(sku).startswith("Retail packaging") else _group(sku))
        for sku in names
    }
    valid = {}
    mismatched = []
    for day, record in records.items():
        if record.timezone != current.timezone:
            mismatched.append(day)
        elif record.complete and not any(
            row.units > 0 and row.sku not in known for row in record.rows
        ):
            valid[day] = record
    seven_units: dict[str, int] = defaultdict(int)
    for record in valid.values():
        for sku, units in _row_units(record).items():
            seven_units[sku] += units
    current_units = _row_units(current)
    channel_units: dict[tuple[str, str], int] = defaultdict(int)
    unknown_channels = set()
    for row in current.rows:
        channel_units[(row.sku, row.channel)] += row.units
        if row.units > 0 and row.sku not in known:
            unknown_channels.add(row.channel)
    for channel, source in current.sources.items():
        if source.get("issues"):
            unknown_channels.add(channel)
    return _View(
        names,
        categories,
        tracked_count,
        frozenset(unknown_names),
        not current.complete or bool(unknown_channels),
        frozenset(unknown_channels),
        current_units,
        dict(channel_units),
        dict(seven_units),
        window,
        tuple(sorted(valid)),
        tuple(day for day in window if day not in valid),
        tuple(sorted(mismatched)),
        len(valid) == 7,
    )


def _coverage(view: _View) -> str:
    return (
        f"7-day window: {view.window[0]} through {view.window[-1]}; "
        f"{len(view.available)}/7 complete reporting dates."
    )


def _missing(view: _View) -> str | None:
    if not view.missing:
        return None
    text = "7-day averages unavailable. Missing: " + ", ".join(map(str, view.missing)) + "."
    if view.mismatched:
        text += " Online timezone differs on: " + ", ".join(map(str, view.mismatched)) + "."
    return text


def _target_variance(current) -> str | None:
    variance = current.sources["Target"].get("units_variance", 0)
    if not variance:
        return None
    if type(variance) is not int:
        raise ValueError("Invalid Target source variance")
    quantity = abs(variance)
    return (
        f"Target product total differs from its headline by {quantity:,} "
        f"unit{'s' if quantity != 1 else ''}; product-row units are used."
    )


def _ordered_skus(view: _View):
    for group in _SECTION_ORDER:
        yield from (sku for sku in view.names if view.categories[sku] == group)


def _merchant_units(merchant: VerifiedDay | None) -> dict[str, int]:
    result: dict[str, int] = defaultdict(int)
    if merchant is not None:
        for row in merchant.rows:
            result[row.sku] += row.units
    return dict(result)


def render_sales_report(
    current: SalesDay,
    history: Mapping[date, SalesDay],
    merchant: VerifiedDay | None = None,
    catalog_path: Path | None = None,
) -> str:
    """Render a complete SKU list; missing or differently timed history is not zero."""
    view = _view(current, history, catalog_path)
    totals = {
        channel: sum(row.units for row in current.rows if row.channel == channel)
        for channel in CHANNELS
    }
    total = sum(view.current_units.values())
    average = f"*{sum(view.seven_units.values()) / 7:,.1f}/day*" if view.complete_average else "n/a"
    headline = (
        "Total reported units: n/a (product mappings unverified)"
        if view.current_unverified
        else f"Total reported units: *{total:,}*"
    )
    count = f"{view.tracked_count} tracked SKUs"
    if view.unknown:
        count += (
            f" + {len(view.unknown)} additional sold SKU{'s' if len(view.unknown) != 1 else ''}"
        )
    channels = " · ".join(
        f"{channel} {'n/a' if channel in view.unverified_channels else f'{totals[channel]:,}'}"
        for channel in CHANNELS
    )
    lines = [
        f"*Daily SKU Sales Velocity | {current.day}*",
        f"{headline} | 7-day average: {average}",
        f"Online reporting day: {_text(current.timezone)}. Target follows its retail report date.",
        f"{count}. Each row: *day units* | *7-day average units/day*; channel counts follow.",
        f"Day channels: {channels}",
        _coverage(view),
    ]
    if missing := _missing(view):
        lines.append(missing)
    collisions = Counter(name.casefold().strip() for name in view.names.values())
    for group in _SECTION_ORDER:
        skus = [sku for sku in view.names if view.categories[sku] == group]
        if not skus:
            continue
        lines.extend(("", f"*{group}*"))
        for sku in skus:
            label = _text(view.names[sku])
            if sku in view.unknown or collisions[view.names[sku].casefold().strip()] > 1:
                label += f" (`{_text(sku)}`)"
            rate = (
                f"{view.seven_units.get(sku, 0) / 7:,.1f}/day" if view.complete_average else "n/a"
            )
            qualifier = " (mapping unverified)" if sku in view.unknown else ""
            lines.append(f"• {label}: *{view.current_units.get(sku, 0):,}* | {rate}{qualifier}")
            lines.append(
                "  "
                + " · ".join(
                    f"{channel} {view.channel_units.get((sku, channel), 0):,}"
                    for channel in CHANNELS
                )
            )
    lines.extend(
        (
            "",
            "Online counts are units ordered, including free items, before returns. "
            "Cancelled online orders are excluded. Target units follow its retail report.",
            "Known Shopify creator-gifting and PR orders are excluded from sales counts; free items within customer orders remain included.",
            "Packs and bundles are counted once through their components. "
            "TikTok includes TikTok fulfillment; Amazon includes Amazon and merchant fulfillment.",
            _RECIPE_HISTORY,
            "Stockouts can limit observed sales. Target replenishment shipments are not retail sales.",
        )
    )
    if view.current_unverified or view.unknown:
        lines.append(
            "Unverified product mappings are visible in Other sold SKUs; do not treat their raw quantities as confirmed physical-unit totals."
        )
    if variance := _target_variance(current):
        lines.append(variance)
    if merchant is None:
        lines.append("Warehouse-only units: unavailable; they are separate from platform sales.")
    else:
        lines.append(
            f"Warehouse-only units | {merchant.day} UTC: *{sum(_merchant_units(merchant).values()):,}* "
            "units ordered before cancellation or return adjustments. This separate warehouse reference is not added to platform sales."
        )
    result = "\n".join(lines)
    if len(result) > MAX_SLACK_CHARACTERS:
        raise ValueError(
            "Complete all-channel report exceeds the Slack message limit; no rows were sent"
        )
    return result


_NAVY = "15334B"
_BLUE = "DDEBF7"
_GRAY = "EEF2F6"
_WHITE = "FFFFFF"


def _cell(sheet, row: int, column: int, value: Any):
    cell = sheet.cell(row, column)
    cell.value = _clean(value) if isinstance(value, str) else value
    if isinstance(value, str):
        # Unknown source names and identifiers must remain text, even if they
        # begin with '='. No provider string becomes an Excel formula.
        cell.data_type = "s"
        cell.number_format = "@"
    cell.font = Font(name="Aptos", size=11)
    cell.alignment = Alignment(vertical="top", wrap_text=isinstance(value, str))
    return cell


def _title(sheet, title: str, columns: int):
    sheet.sheet_view.showGridLines = False
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=columns)
    cell = _cell(sheet, 1, 1, title)
    cell.font = Font(name="Aptos", size=18, bold=True, color=_WHITE)
    cell.fill = PatternFill("solid", fgColor=_NAVY)
    for column in range(1, columns + 1):
        sheet.cell(1, column).fill = PatternFill("solid", fgColor=_NAVY)
    sheet.row_dimensions[1].height = 34
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0


def _headers(sheet, row: int, labels):
    for column, label in enumerate(labels, 1):
        cell = _cell(sheet, row, column, label)
        cell.font = Font(name="Aptos", size=11, bold=True, color=_WHITE)
        cell.fill = PatternFill("solid", fgColor=_NAVY)
    sheet.row_dimensions[row].height = 32


def _row_height(values, widths, minimum: int) -> float:
    lines = max(
        (
            math.ceil(len(_clean(value)) / max(width * 0.9, 1))
            for value, width in zip(values, widths, strict=True)
        ),
        default=1,
    )
    return min(max(minimum, lines * 15), 409)


def _source_rows(current, merchant):
    rows = []
    definitions = {
        "Shopify": "Units ordered on Shopify; packs and bundles count through their contents. Cancelled orders, gift cards, recognized marketplace mirrors and known creator-gifting/PR orders excluded; free items within customer orders included; before returns.",
        "TikTok": "Units ordered on TikTok Shop across seller and TikTok fulfillment. Packs and bundles count through their contents; cancelled orders excluded; free items included; before returns.",
        "Amazon": "Units ordered on Amazon across Amazon and merchant fulfillment. Packs and bundles count through their contents; cancelled orders excluded; before returns.",
        "Target": "Units sold in the dated retailer report, derived from product rows; Target replenishment shipments excluded.",
    }
    labels = {
        "captured_at": "When this source was collected.",
        "data_through": "Latest sales time available in the source when collected.",
        "listing_units": "Source units before source exclusions and pack or bundle component expansion.",
        "control_listing_units": "Independent total before pack and bundle component expansion.",
        "source_row_count": "Number of complete source rows read.",
        "component_units": "Sales units after source exclusions and pack or bundle component expansion.",
        "excluded_mirrored_listing_units": "Shopify listing units from recognized marketplace mirrors, excluded to avoid adding them again.",
        "excluded_promotional_listing_units": "Shopify creator-gifting and PR listing units excluded from sales, before pack expansion.",
        "raw_sha256": "Hash identifying the exact captured source data.",
        "provider_query": "Source report link or query reference.",
        "scope": "Orders and exclusions covered by this source.",
        "message_id": "Target source email identifier.",
        "previous_message_id": "Earlier Target source email used for the daily change.",
        "reported_daily_units": "Target's separate daily headline total.",
        "derived_daily_units": "Sum of Target product-row daily units used in this report.",
        "units_variance": "Product-row daily units minus Target's daily headline.",
    }
    for channel in CHANNELS:
        source = current.sources[channel]
        rows.extend(
            [
                (channel, "Report date", current.day.isoformat(), "Source reporting date."),
                (
                    channel,
                    "Reporting day",
                    "Retail report date" if channel == "Target" else current.timezone,
                    "Target follows its retail report date; online source days follow the named timezone.",
                ),
                (
                    channel,
                    "Count definition",
                    definitions[channel],
                    "Counts are units, not numbers of orders.",
                ),
                (
                    channel,
                    "Source status",
                    source.get("status", "unavailable"),
                    "All source rows and controls must be complete.",
                ),
            ]
        )
        for key, meaning in labels.items():
            value = source.get(key)
            if value is not None:
                if isinstance(value, Mapping):
                    value = "; ".join(
                        f"{_clean(name).replace('_', ' ')}: {_clean(item)}"
                        for name, item in sorted(value.items())
                    )
                rows.append((channel, key, value, meaning))
        for issue in source.get("issues", ()):
            rows.append(
                (channel, "Mapping issue", issue, "Physical product mapping requires confirmation.")
            )
    if variance := _target_variance(current):
        rows.append(
            (
                "Target",
                "Product/headline difference",
                variance,
                "The product-row total is used; the headline is not silently forced to match.",
            )
        )
    if merchant is not None:
        rows.extend(
            [
                (
                    "Warehouse-only",
                    "Report date",
                    merchant.day.isoformat(),
                    "Separate complete UTC order date.",
                ),
                ("Warehouse-only", "Reporting day", "UTC", "Not added to platform sales totals."),
                (
                    "Warehouse-only",
                    "Count definition",
                    "Merchdrop Shopify, TikTok and Amazon FBM component units ordered, before cancellation or return adjustments.",
                    "A separate warehouse reference, not additional sales.",
                ),
                (
                    "Warehouse-only",
                    "Source orders checked",
                    merchant.coverage.fetched_unique,
                    "Includes orders outside the customer-sales channel list.",
                ),
                (
                    "Warehouse-only",
                    "Captured at",
                    merchant.captured_at.isoformat(),
                    "Time of the checked warehouse source capture.",
                ),
            ]
        )
    return rows


def build_sales_workbook(
    current: SalesDay,
    history: Mapping[date, SalesDay],
    merchant: VerifiedDay | None = None,
    catalog_path: Path | None = None,
) -> bytes:
    """Create the saved report with source definitions and no hidden raw data."""
    view = _view(current, history, catalog_path)
    workbook = openpyxl.Workbook()
    workbook.properties.title = f"BASED Daily SKU Sales Velocity | {current.day}"
    workbook.properties.subject = "Daily unit counts across Shopify, TikTok, Amazon and Target"
    sheet = workbook.active
    sheet.title = "SKU Velocity"
    _title(sheet, f"BASED Daily SKU Sales Velocity | {current.day}", 10)
    descriptions = [
        f"Online reporting day: {current.timezone}. Target follows its retail report date.",
        "Each size, scent and retail packaging SKU stays separate. Packs and bundles count once through components.",
        "Total reported units: "
        + (
            "n/a (product mappings unverified)"
            if view.current_unverified
            else f"{sum(view.current_units.values()):,}"
        )
        + " | 7-day average: "
        + (f"{sum(view.seven_units.values()) / 7:,.1f}/day" if view.complete_average else "n/a"),
        _coverage(view),
        _missing(view)
        or "Seven-day average: total units across the seven complete reporting dates divided by seven.",
    ]
    for row, text in enumerate(descriptions, 2):
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=10)
        _cell(sheet, row, 1, text)
        sheet.row_dimensions[row].height = 30
    warehouse_label = (
        f"Warehouse-only units ({merchant.day} UTC)"
        if merchant
        else "Warehouse-only units (unavailable)"
    )
    headers = [
        "Category",
        "Product",
        "SKU",
        "Day units",
        "7-day average/day",
        *CHANNELS,
        warehouse_label,
    ]
    _headers(sheet, 8, headers)
    warehouse_units = _merchant_units(merchant)
    widths = [27, 42, 34, 14, 22, 13, 13, 13, 13, 30]
    for row_number, sku in enumerate(_ordered_skus(view), 9):
        values = [
            view.categories[sku],
            view.names[sku],
            sku,
            view.current_units.get(sku, 0),
            view.seven_units.get(sku, 0) / 7 if view.complete_average else "n/a",
            *(view.channel_units.get((sku, channel), 0) for channel in CHANNELS),
            warehouse_units.get(sku, 0) if merchant is not None else "n/a",
        ]
        for column, value in enumerate(values, 1):
            cell = _cell(sheet, row_number, column, value)
            if column >= 4 and isinstance(value, int | float):
                cell.number_format = "#,##0.0" if column == 5 else "#,##0"
            if row_number % 2 == 0:
                cell.fill = PatternFill("solid", fgColor=_GRAY)
        sheet.row_dimensions[row_number].height = _row_height(values, widths, 32)
    last_row = 8 + len(view.names)
    sheet.freeze_panes = "D9"
    sheet.auto_filter.ref = f"A8:J{last_row}"
    sheet.print_title_rows = "1:8"
    sheet.print_area = f"A1:J{last_row}"
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.sheet_view.zoomScale = 85

    sources = workbook.create_sheet("Sources")
    _title(sources, f"Sales sources | {current.day}", 4)
    _headers(sources, 3, ["Source", "Field", "Value", "Meaning"])
    for row_number, values in enumerate(_source_rows(current, merchant), 4):
        for column, value in enumerate(values, 1):
            _cell(sources, row_number, column, value)
        sources.row_dimensions[row_number].height = _row_height(values, (20, 27, 73, 68), 48)
    for column, width in zip("ABCD", (20, 27, 73, 68), strict=True):
        sources.column_dimensions[column].width = width
    sources.freeze_panes = "C4"
    sources.auto_filter.ref = f"A3:D{sources.max_row}"
    sources.print_title_rows = "1:3"

    methods = workbook.create_sheet("Methods")
    _title(methods, f"Sales count definitions | {current.day}", 2)
    _headers(methods, 3, ["Topic", "Definition"])
    method_rows = [
        (
            "Reported units",
            "Online counts are units ordered, including free items, before returns. Cancelled online orders are excluded. Known Shopify creator-gifting and PR orders are excluded from sales; free items within customer orders remain included. Target units follow its retail report.",
        ),
        (
            "Reporting day",
            f"Online days run from midnight to midnight in {current.timezone}. Target uses the date printed on its retailer report. The combined count joins those source calendar dates.",
        ),
        (
            "Source cutoff",
            "Sources lists when each source was collected and its latest available sales time. Each required report date must match this report date, and collection must occur after that source date ends.",
        ),
        (
            "Channel coverage",
            "TikTok includes TikTok fulfillment; Amazon includes Amazon and merchant fulfillment. Target covers store sales, not replenishment shipments.",
        ),
        (
            "Packs and bundles",
            "Each sold pack or bundle listing expands into its physical components. Separately purchased loose units or child kits remain additional. "
            + _RECIPE_HISTORY,
        ),
        (
            "Seven-day average",
            f"The window is {view.window[0]} through {view.window[-1]}. Sum seven complete source dates and divide by seven. All online timezones must match the current reporting day.",
        ),
        (
            "History coverage",
            _coverage(view) + " " + (_missing(view) or "All seven source dates are complete."),
        ),
        (
            "Zero and unavailable",
            "0 means no units were recorded under that SKU in the complete source. n/a means the average or separate warehouse reference is unavailable. Missing dates are never filled with zero.",
        ),
        (
            "Unverified mappings",
            "Additional sold SKUs remain visible. If a positive product mapping is unverified, the headline physical-unit total and seven-day average are withheld until the mapping is confirmed.",
        ),
        (
            "Warehouse-only reference",
            "The separate Merchdrop reference uses complete UTC order dates before cancellation or return adjustments. It is not added to the all-channel sales counts. Its date and scope are shown in the column label and Sources."
            if merchant is not None
            else "Warehouse-only units are unavailable for this report and are not included in all-channel sales counts.",
        ),
        (
            "Observed sales",
            "Stockouts can limit observed sales. These are observed unit counts, not a forecast of unrestricted demand.",
        ),
    ]
    if variance := _target_variance(current):
        method_rows.append(("Target headline variance", variance))
    for row_number, values in enumerate(method_rows, 4):
        for column, value in enumerate(values, 1):
            _cell(methods, row_number, column, value)
        methods.row_dimensions[row_number].height = _row_height(values, (29, 118), 52)
    methods.column_dimensions["A"].width = 29
    methods.column_dimensions["B"].width = 118
    methods.freeze_panes = "B4"
    methods.print_title_rows = "1:3"
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()
