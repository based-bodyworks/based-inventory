"""Plan merchant outputs from one saved version; never write formula spill cells."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import date

from .models import CHANNELS, VerifiedDay

HISTORY_HEADER = [
    "Date",
    "Product",
    "Units",
    "SKU",
    "Channel",
    "Date SKU channel key",
    "Source",
    "Verified on",
]
DAILY_HEADER = [
    "Date",
    "TikTok units",
    "Shopify units",
    "Amazon FBM units",
    "Velocity total",
    "Target units",
    "Target retail sales",
    "Merchant source",
    "Target report",
    "Timing / scope",
]


def serial(day: date) -> int:
    return (day - date(1899, 12, 30)).days


def cell(value):
    if value is None or value == "":
        return {}
    if type(value) in (float, int):
        if not math.isfinite(value):
            raise ValueError("Non-finite cell quantity")
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value)}}


def update(sheet_id, row, column, values, *, fields="userEnteredValue"):
    return {
        "updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": column},
            "rows": [{"values": [cell(v) for v in values]}],
            "fields": fields,
        }
    }


def pad(row, width):
    return list(row[:width]) + [None] * max(0, width - len(row))


def equal_rows(left, right):
    if len(left) != len(right):
        return False
    for a, b in zip(left, right, strict=True):
        for x, y in zip(pad(a, max(len(a), len(b))), pad(b, max(len(a), len(b))), strict=True):
            if x in (None, "") and y in (None, ""):
                continue
            if type(x) in (int, float) and type(y) in (int, float):
                if not math.isclose(x, y, rel_tol=1e-10, abs_tol=1e-8):
                    return False
            elif x != y:
                return False
    return True


def build_merchant_plan(
    version: VerifiedDay, history, rolling, daily, product_map, source_url: str, notes: dict
) -> dict:
    if (
        not history
        or history[0][:8] != HISTORY_HEADER
        or not daily
        or daily[0][:10] != DAILY_HEADER
    ):
        raise ValueError("Tracker headers differ from the approved schema")
    history_after = [pad(r, 8) for r in history]
    daily_after = [pad(r, 10) for r in daily]
    rolling_after = [pad(r, 20) for r in rolling]
    indexes, dynamic, erp = {}, [], []
    for index, row in enumerate(history_after[1:], 1):
        if (
            len(row) < 5
            or type(row[0]) not in (int, float)
            or type(row[2]) not in (int, float)
            or row[2] < 0
        ):
            raise ValueError("Malformed durable history row")
        key = (row[0], str(row[3]), row[4])
        if key in indexes:
            raise ValueError("duplicate durable history key")
        indexes[key] = index
    day = serial(version.day)
    source = {(r.sku, r.channel): r for r in version.rows}
    day_keys = {key[1:] for key in indexes if key[0] == day} | set(source)
    if not product_map or len(product_map[0]) < 2:
        raise ValueError("Missing product mapping for zero-channel coverage")
    marker_sku, marker_product = str(product_map[0][0]), product_map[0][1]
    for channel in CHANNELS:
        if not any(key[1] == channel for key in day_keys):
            day_keys.add((marker_sku, channel))
    for sku, channel in sorted(day_keys):
        if channel not in CHANNELS:
            raise ValueError("Unexpected merchant history channel")
        index = indexes.get((day, sku, channel))
        original = pad(history_after[index], 8) if index is not None else [None] * 8
        record = source.get((sku, channel))
        units = record.units if record else 0
        row = [
            day,
            record.product if record else (original[1] or marker_product),
            units,
            sku,
            channel,
            f"{version.day}|{sku}|{channel}",
            source_url,
            version.captured_at.date().isoformat(),
        ]
        if index is None:
            index = len(history_after)
            history_after.append(row)
        else:
            history_after[index] = row
        if row != original:
            dynamic.append(update(2106291001, index, 0, row))
            if original[2] is not None and original[2] != units:
                previous = notes.get(f"C{index+1}", "")
                audit = f"{version.captured_at.isoformat()}: {original[2]} units replaced with {units}. Verified sales version {version.version_id}; source orders {version.coverage.fetched_unique}; report {source_url}."
                dynamic.append(
                    {
                        "updateCells": {
                            "start": {"sheetId": 2106291001, "rowIndex": index, "columnIndex": 2},
                            "rows": [
                                {
                                    "values": [
                                        {"note": previous + ("\n" if previous else "") + audit}
                                    ]
                                }
                            ],
                            "fields": "note",
                        }
                    }
                )
    mapping = {}
    for row in product_map:
        if (
            len(row) < 3
            or type(row[2]) not in (int, float)
            or not math.isfinite(row[2])
            or row[2] < 0
            or str(row[0]) in mapping
        ):
            raise ValueError("Invalid or duplicate product mapping")
        mapping[str(row[0])] = (row[1], row[2])
    # Repairing an older report must preserve the current demand window.
    window_end = max(day, rolling_after[1][1])
    if type(window_end) not in (int, float) or not float(window_end).is_integer():
        raise ValueError("Invalid rolling window end")
    totals, controls = defaultdict(float), Counter()
    for r in history_after[1:]:
        if window_end - 6 <= r[0] <= window_end and r[4] in CHANNELS:
            controls[(r[0], r[4])] += 1
            target = mapping.get(str(r[3]))
            if not target:
                if r[2]:
                    raise ValueError("Positive sold SKU has no product mapping")
                continue
            totals[(target[0], r[4])] += r[2] * target[1]
    required = {(window_end - n, channel) for n in range(7) for channel in CHANNELS}
    if not required <= controls.keys():
        raise ValueError("Incomplete seven-day merchant history")
    catalog = {r[0] for r in rolling_after[7:] if r[0]}
    if not catalog or any(
        value > 0 and product not in catalog for (product, _), value in totals.items()
    ):
        raise ValueError("Positive product missing from rolling catalog mapping")
    for row in rolling_after[7:]:
        if row[0]:
            quantities = [totals[(row[0], channel)] for channel in CHANNELS]
            total = sum(quantities)
            row[2:10] = [*quantities, total, total / 7, window_end - 6, window_end, "Complete"]
        if row[15] and row[16]:
            target = mapping.get(str(row[15]))
            if not target:
                raise ValueError("Missing reverse product mapping")
            row[17] = totals[(target[0], row[16])]
    for offset, ((d, ch), count) in enumerate(sorted(controls.items()), 7):
        while offset >= len(rolling_after):
            rolling_after.append([None] * 20)
        rolling_after[offset][11:14] = [d, ch, count]
    rolling_after[1][1], rolling_after[2][1], rolling_after[4][1] = (
        window_end,
        window_end - 6,
        "Complete",
    )
    for index, row in enumerate(rolling_after):
        if not equal_rows([row], [pad(rolling[index], 20) if index < len(rolling) else []]):
            dynamic.append(update(1740410291, index, 0, row))
    matching = [i for i, r in enumerate(daily_after[1:], 1) if r[0] == day]
    if len(matching) > 1:
        raise ValueError("duplicate ERP daily sales date")
    index = matching[0] if matching else len(daily_after)
    if index == len(daily_after):
        daily_after.append([None] * 10)
    row = daily_after[index]
    if matching:
        note = row[9] or ""
    else:
        note = "Target retail data is pending; blank means unverified, not zero."
    row[0:5] = [
        day,
        version.totals["TIKTOK"],
        version.totals["Shopify"],
        version.totals["Amazon FBM"],
        sum(version.totals.values()),
    ]
    row[7] = source_url
    marker = " Merchant verification: "
    note = note.split(marker)[0]
    target = re.search(r"(?:^|(?<=[.!?])\s+)Target (?!replenishment)", note)
    note = (
        note[target.start() :].strip()
        if target
        else "Target retail data is reported separately; blank values are unverified, not zero."
    )
    row[9] = (
        note
        + marker
        + f"UTC day {version.day}; {version.coverage.fetched_unique} raw warehouse orders matched before and after complete order and line pagination; {sum(version.excluded_orders.values())} non-sales orders excluded. Version {version.version_id}. Ordered merchant units exclude Amazon FBA and Target replenishment; Target retail is separate."
    )
    erp.extend(
        [
            update(1275479413, index, 0, row[:5]),
            update(1275479413, index, 7, [row[7]]),
            update(1275479413, index, 9, [row[9]]),
        ]
    )
    return {
        "version_id": version.version_id,
        "dynamic_requests": dynamic,
        "erp_requests": erp,
        "history_after": history_after,
        "rolling_after": rolling_after,
        "daily_after": daily_after,
    }


def validate_plan_readback(plan, history, rolling, daily):
    for label, expected, actual in (
        ("history", plan["history_after"], history),
        ("rolling", plan["rolling_after"], rolling),
        ("daily", plan["daily_after"], daily),
    ):
        if not equal_rows(expected, actual):
            raise ValueError(f"Native {label} readback differs from verified source plan")
