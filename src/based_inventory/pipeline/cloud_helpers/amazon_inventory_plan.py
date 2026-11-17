#!/usr/bin/env python3
"""Validate listing inventory, select recent gaps and plan bounded native writes.

This helper never calls a provider or writes a spreadsheet. Inventory listing
quantities stay exactly as reported. Sales are used only to choose relevant rows.
"""

import argparse
import json
import math
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

SPREADSHEET_ID = "1vI9dM06MtptojGSxBAfNqOu0l0ZPyAGj0-D88ZF82eY"
SHEET_ID = 1740410294
TABLE_ID = "1380483092"
DATASOURCE_ID = "223f61d4-92f0-408a-9c2b-c3b42a8e53c8"
ORDERED_UNITS_METRIC = "amazonsp_order_items.raw.quantity_ordered"
QUANTITIES = (
    "fulfillable",
    "reserved",
    "inbound_working",
    "inbound_shipped",
    "inbound_receiving",
    "unfulfillable",
    "total",
)


def unwrap(value):
    if "structuredContent" in value:
        return value["structuredContent"]
    if "rows" not in value and value.get("content"):
        return json.loads(next(x["text"] for x in value["content"] if x["type"] == "text"))
    return value


def build_plan(snapshot, sales, annotations, start, end, now=None, previous_count=48):
    snapshot, sales = unwrap(snapshot), unwrap(sales)
    now = now or datetime.now(UTC)
    start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
    if (end_day - start_day).days != 6:
        raise ValueError("Sales selection requires seven complete dates")
    if end_day != now.astimezone(UTC).date() - timedelta(days=1):
        raise ValueError("Sales window must contain the latest seven completed UTC days")
    if snapshot.get("truncated") is not False:
        raise ValueError("Inventory response is truncated or completeness is unknown")
    if (
        snapshot.get("connector_key") != "amazon-selling-partner"
        or snapshot.get("operation") != "inventory"
    ):
        raise ValueError("Wrong inventory source operation")
    if snapshot.get("datasource_id") != DATASOURCE_ID:
        raise ValueError("Wrong connected Amazon account")
    captured = datetime.fromisoformat(snapshot["fetched_at"].replace("Z", "+00:00"))
    age = (now - captured).total_seconds()
    if age < -300 or age > 30 * 60:
        raise ValueError("Inventory capture is stale or in the future")
    rows = snapshot["rows"]
    if snapshot.get("row_count") != len(rows) or not rows:
        raise ValueError("Inventory row count is incomplete")
    seen = set()
    for row in rows:
        key = (row["marketplace_id"], row["sku"], row["condition"])
        if key in seen or row["marketplace_id"] != "ATVPDKIKX0DER":
            raise ValueError("Duplicate listing key or wrong marketplace")
        seen.add(key)
        for field in QUANTITIES:
            if type(row.get(field)) is not int or row[field] < 0:
                raise ValueError("Missing or invalid inventory quantity: " + field)
        if row["total"] != sum(row[field] for field in QUANTITIES if field != "total"):
            raise ValueError("Provider total does not equal the inventory states: " + row["sku"])
    expected_scope = dict(
        query_timezone="UTC",
        marketplace="ATVPDKIKX0DER",
        sales_channel="Amazon.com",
        fulfillment_channel="AFN",
        status_excluded="Canceled",
    )
    if any(sales.get(k) != v for k, v in expected_scope.items()):
        raise ValueError("Wrong AFN sales scope")
    if sales.get("metric") != ORDERED_UNITS_METRIC:
        raise ValueError("Wrong ordered listing unit metric")
    if sales.get("truncated") is not False or sales.get("row_limit_reached") is not False:
        raise ValueError("Sales response is truncated or incomplete")
    if sales["date_from"] > start or sales["date_to"] < end:
        raise ValueError("Sales date coverage is incomplete")
    units, dates, sales_keys = Counter(), set(), set()
    for row in sales["rows"]:
        if start <= row["date"] <= end:
            key = (row["date"], row["sku"])
            if key in sales_keys:
                raise ValueError("Duplicate sales date/SKU")
            sales_keys.add(key)
            quantity = row[sales["metric"]]
            if (
                type(quantity) not in (int, float)
                or not math.isfinite(quantity)
                or quantity < 0
                or quantity != int(quantity)
            ):
                raise ValueError("Invalid ordered listing units")
            units[row["sku"]] += quantity
            dates.add(row["date"])
    expected_dates = {(start_day + timedelta(days=i)).isoformat() for i in range(7)}
    if dates != expected_dates:
        raise ValueError("Sales day coverage is incomplete")
    inventory_skus = {r["sku"] for r in rows}
    if any(sku not in inventory_skus for sku, quantity in units.items() if quantity > 0):
        raise ValueError("A recently sold listing is missing from inventory")
    gaps, recent_singles = [], []
    for row in rows:
        if units[row["sku"]] <= 0:
            continue
        identity = annotations.get(row["asin"])
        if not identity or identity.get("provider_product_title") != row["product_name"]:
            raise ValueError("Unverified recent listing identity: " + row["sku"])
        if identity["kind"] not in ("single", "kit", "multipack"):
            raise ValueError("Unverified listing kind: " + row["sku"])
        if identity["kind"] != "single":
            continue
        alternate_rows = [r for r in rows if r["asin"] == row["asin"] and r["sku"] != row["sku"]]
        positive_alternates = [r["sku"] for r in alternate_rows if r["fulfillable"] > 0]
        same_product_positive = [
            r["sku"]
            for r in rows
            if r["sku"] != row["sku"]
            and r["fulfillable"] > 0
            and annotations.get(r["asin"], {}).get("label") == identity["label"]
        ]
        result = dict(
            row,
            label=identity["label"],
            units7=units[row["sku"]],
            inbound=row["inbound_working"] + row["inbound_shipped"] + row["inbound_receiving"],
            positive_ready_alternates=positive_alternates,
            positive_product_alternates=same_product_positive,
            product_zero_ready=(row["fulfillable"] == 0 and not same_product_positive),
        )
        recent_singles.append(result)
        if row["fulfillable"] == 0:
            gaps.append(result)
    expected = [
        [
            r["marketplace_id"],
            r["sku"],
            r["asin"],
            r["product_name"],
            r["condition"],
            r["fulfillable"],
            r["reserved"],
            r["inbound_working"] + r["inbound_shipped"] + r["inbound_receiving"],
            r["inbound_working"],
            r["inbound_shipped"],
            r["inbound_receiving"],
            r["unfulfillable"],
            r["total"],
            snapshot["fetched_at"],
            "Amazon Selling Partner API inventory",
            snapshot["account_label"],
            snapshot["datasource_id"],
        ]
        for r in rows
    ]

    def cell(v):
        return {
            "userEnteredValue": {"numberValue": v}
            if type(v) in (int, float)
            else {"stringValue": v}
        }

    requests = [
        {
            "updateCells": {
                "range": {
                    "sheetId": SHEET_ID,
                    "startRowIndex": 8,
                    "endRowIndex": 8 + max(previous_count, len(rows)),
                    "startColumnIndex": 0,
                    "endColumnIndex": 17,
                },
                "rows": [{"values": [cell(v) for v in r]} for r in expected],
                "fields": "userEnteredValue",
            }
        },
        {
            "updateCells": {
                "start": {"sheetId": SHEET_ID, "rowIndex": 1, "columnIndex": 1},
                "rows": [{"values": [cell(snapshot["fetched_at"])]}],
                "fields": "userEnteredValue",
            }
        },
        {
            "updateCells": {
                "start": {"sheetId": SHEET_ID, "rowIndex": 5, "columnIndex": 1},
                "rows": [
                    {
                        "values": [
                            cell(
                                f"{len(rows)} unique listing rows. Complete provider response, not truncated."
                            )
                        ]
                    }
                ],
                "fields": "userEnteredValue",
            }
        },
        {
            "updateCells": {
                "start": {"sheetId": SHEET_ID, "rowIndex": 6, "columnIndex": 1},
                "rows": [{"values": [cell(" ".join(snapshot.get("notes", [])))]}],
                "fields": "userEnteredValue",
            }
        },
        {
            "updateTable": {
                "table": {
                    "tableId": TABLE_ID,
                    "range": {
                        "sheetId": SHEET_ID,
                        "startRowIndex": 7,
                        "endRowIndex": 8 + len(rows),
                        "startColumnIndex": 0,
                        "endColumnIndex": 17,
                    },
                },
                "fields": "range",
            }
        },
    ]
    return dict(
        schema="based.amazon-inventory-plan.v1",
        captured_at=snapshot["fetched_at"],
        source_notes=snapshot.get("notes", []),
        window_start=start,
        window_end=end,
        listing_count=len(rows),
        recent_singles=recent_singles,
        zero_ready_recent_singles=gaps,
        expected_readback=expected,
        batch_update=dict(spreadsheet_id=SPREADSHEET_ID, requests=requests),
    )


def verify_readback(plan, rows):
    if rows != plan["expected_readback"]:
        raise ValueError("Native inventory readback differs from the captured listing rows")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("inventory", "sales", "annotations", "start", "end", "output"):
        parser.add_argument("--" + arg, required=True)
    parser.add_argument("--previous-count", type=int, required=True)
    parser.add_argument("--readback")
    args = parser.parse_args()

    def read(p):
        return json.loads(Path(p).read_text())

    plan = build_plan(
        read(args.inventory),
        read(args.sales),
        read(args.annotations),
        args.start,
        args.end,
        previous_count=args.previous_count,
    )
    if args.readback:
        value = unwrap(read(args.readback))
        verify_readback(plan, value["values"] if isinstance(value, dict) else value)
        plan["readback_verified"] = True
    Path(args.output).write_text(json.dumps(plan, indent=2) + "\n")
    print(
        json.dumps(
            {k: plan[k] for k in ("captured_at", "listing_count", "window_start", "window_end")}
        )
    )


if __name__ == "__main__":
    main()
