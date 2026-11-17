#!/usr/bin/env python3
"""Parse the newest complete Target report and reconcile daily product changes.

Input is a plaintext MIME body or a saved Gmail read result. Product rows are
dynamic: unknown labels remain visible and block automatic writes until mapped.
This helper reads sources and writes JSON only. It never edits Google or Excel.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path


class ReportError(ValueError):
    pass


def number(token):
    cleaned = token.replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ReportError("Invalid numeric table cell: " + token) from exc
    if "%" in token:
        return float(value / 100)
    return int(value) if value == value.to_integral() else float(value)


def normalized(label):
    return re.sub(r"[^A-Z0-9]", "", label.upper())


def plaintext(source):
    if isinstance(source, str):
        return source
    source = source.get("structuredContent", source)

    def visit(part):
        if part.get("mime_type", part.get("mimeType")) == "text/plain":
            return part.get("body", {}).get("content", "")
        return "\n".join(visit(child) for child in part.get("parts", []))

    body = visit(source.get("payload", source))
    if not body:
        raise ReportError("A plaintext MIME body is required")
    return body


def parse_report(source, report_date, message_id=""):
    date = dt.date.fromisoformat(report_date)
    text = unicodedata.normalize("NFKC", plaintext(source)).replace("\r", "")
    newest = re.split(
        r"(?im)^From:\s|^On [^\n]{0,250}wrote:\s*$|^-+\s*Original Message\s*-+", text, maxsplit=1
    )[0]
    raw = source.get("structuredContent", source) if isinstance(source, dict) else {}
    headers = raw.get("payload", raw).get("headers", [])
    subject = next(
        (x.get("value", "") for x in headers if x.get("name", "").lower() == "subject"), ""
    )
    date_pattern = r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?"
    subject_match = re.search(r"Updated\s+through\s*" + date_pattern, subject, re.I)
    body_match = re.search(r"Daily\s+Sales\s+as\s+of\s*" + date_pattern, newest, re.I)

    def matches_requested(match):
        if not match:
            return False
        year = int(match.group(3)) if match.group(3) else date.year
        if year < 100:
            year += 2000
        return (year, int(match.group(1)), int(match.group(2))) == (date.year, date.month, date.day)

    date_note = ""
    if subject_match and not matches_requested(subject_match):
        raise ReportError("Subject report date does not match the requested date")
    if body_match and not matches_requested(body_match):
        if not matches_requested(subject_match):
            raise ReportError("Newest body report date does not match the requested date")
        date_note = "Verified subject date used because the newest body date differs"
    if not body_match and not matches_requested(subject_match):
        raise ReportError("Report has no verifiable date")
    marker = re.search(r"Week\s+to\s+Date\s+Performance\s*:", newest, re.I)
    if not marker:
        raise ReportError("Newest email section has no complete product table")
    summary = newest[: marker.start()]
    reported = re.search(r"([\d,]+)\s+units sold yesterday", summary, re.I)
    if not reported:
        raise ReportError("Newest section has no reported daily unit count")
    lines = [x.strip() for x in newest[marker.end() :].splitlines() if x.strip()]
    numeric = re.compile(r"^-?\$?\s*[\d,]+(?:\.\d+)?%?$")
    try:
        start = next(
            i
            for i, line in enumerate(lines[:-1])
            if re.search(r"[A-Za-z]", line)
            and not line.startswith("[cid:")
            and numeric.fullmatch(lines[i + 1])
            and lines[i + 1].lstrip().startswith("$")
        )
    except StopIteration as exc:
        raise ReportError("Product table lacks its complete header") from exc
    headers = lines[:start]
    if not headers or normalized(headers[0]) != "ROWLABELS":
        raise ReportError("Product table lacks its Row Labels header")
    value_headers = headers[1:]
    header_keys = [normalized(x) for x in value_headers]

    def header_index(*keys):
        for key in keys:
            if key in header_keys:
                return header_keys.index(key)
        raise ReportError("Product table lacks required column: " + "/".join(keys))

    retail_i = header_index("TY")
    units_i = header_index("UTY")
    forecast_i = header_index("UFRST", "UFCST")
    to_forecast_i = header_index("TOFCST")
    asp_i = header_index("ASP")
    oos_i = header_index("OOS")
    products, grand = [], None
    i = start
    while i < len(lines):
        label = lines[i]
        if not re.search(r"[A-Za-z]", label) or label.startswith("[cid:"):
            raise ReportError("Unrecognized product table row: " + label)
        i += 1
        values = []
        while i < len(lines) and numeric.fullmatch(lines[i]):
            values.append(lines[i])
            i += 1
        if (
            len(values) < len(value_headers)
            and number(values[forecast_i]) == 0
            and not values[to_forecast_i].endswith("%")
        ):
            values.insert(to_forecast_i, None)
        if (
            len(values) == len(value_headers) - 1
            and number(values[forecast_i]) == 0
            and values[asp_i] is not None
            and not values[asp_i].startswith("$")
        ):
            values.insert(asp_i, None)
        if len(values) == len(value_headers) - 1 and oos_i == len(value_headers) - 1:
            values.append(None)
        if len(values) != len(value_headers) or not values[retail_i].startswith("$"):
            raise ReportError("Incomplete product row: " + label)
        units = number(values[units_i])
        if not isinstance(units, int):
            raise ReportError("Product units must be whole numbers: " + label)
        item = {
            "label": label,
            "week_retail": number(values[retail_i]),
            "week_units": units,
            "week_forecast_units": number(values[forecast_i]),
            "out_of_stock": number(values[oos_i])
            if values[oos_i] is not None and values[oos_i].endswith("%")
            else None,
            "asp": number(values[asp_i]) if values[asp_i] is not None else None,
        }
        if normalized(label) == "GRANDTOTAL":
            grand = item
            break
        products.append(item)
    if not grand or not products:
        raise ReportError("Product table has no complete grand total")
    if len({normalized(x["label"]) for x in products}) != len(products):
        raise ReportError("Duplicate product label in newest table")
    sum_units = sum(x["week_units"] for x in products)
    sum_retail = sum(Decimal(str(x["week_retail"])) for x in products)
    complete = sum_units == grand["week_units"] and sum_retail == Decimal(str(grand["week_retail"]))
    if not complete:
        raise ReportError("Product rows do not reconcile to the current grand total")
    retail_match = re.search(r"daily retail sales[^$\n]*\$([\d,.]+)", summary, re.I)
    summary_asp = re.search(r"(?:summary|daily)\s+ASP[^$\n]*\$([\d,.]+)", summary, re.I)
    return {
        "date": date.isoformat(),
        "message_id": message_id,
        "date_note": date_note,
        "reported_daily_units": number(reported.group(1)),
        "reported_daily_retail": number(retail_match.group(1)) if retail_match else None,
        "summary_asp": number(summary_asp.group(1)) if summary_asp else None,
        "category_complete": complete,
        "products": products,
        "grand": grand,
    }


def sunday_start(date):
    return date - dt.timedelta(days=(date.weekday() + 1) % 7)


def derive_daily(current, previous=None, sku_map=None):
    date = dt.date.fromisoformat(current["date"])
    sunday = date.weekday() == 6
    if not current.get("category_complete"):
        raise ReportError("Current product table is incomplete")
    if not sunday:
        if not previous or not previous.get("category_complete"):
            raise ReportError("A complete prior-day table is required")
        prior_date = dt.date.fromisoformat(previous["date"])
        if prior_date != date - dt.timedelta(days=1) or sunday_start(prior_date) != sunday_start(
            date
        ):
            raise ReportError("Prior report is not the preceding day of the same Sunday-based week")
    prior = {} if sunday else {normalized(p["label"]): p for p in previous["products"]}
    mapping = {normalized(k): v for k, v in (sku_map or {}).items()}
    current_keys = {normalized(x["label"]) for x in current["products"]}
    for key, item in prior.items():
        if key not in current_keys and (item["week_units"] or item["week_retail"]):
            raise ReportError(
                "A prior positive product disappeared from the current table: " + item["label"]
            )
    rows, unmapped = [], []
    for item in current["products"]:
        key = normalized(item["label"])
        old = prior.get(key)
        new_row = not sunday and old is None
        baseline_units = old["week_units"] if old else 0
        baseline_retail = old["week_retail"] if old else 0
        daily_units = item["week_units"] - baseline_units
        daily_retail = Decimal(str(item["week_retail"])) - Decimal(str(baseline_retail))
        if daily_units < 0 or daily_retail < 0:
            raise ReportError("Negative daily change requires source review: " + item["label"])
        mapped = mapping.get(key)
        if not mapped:
            unmapped.append(item["label"])
        method = (
            "Sunday week-to-date equals daily"
            if sunday
            else "Current week-to-date less complete prior-day table"
        )
        if new_row:
            method += "; first appeared this week, prior zero proved by complete prior grand totals"
        rows.append(
            {
                **item,
                "product": mapped.get("product", item["label"]) if mapped else item["label"],
                "sku": mapped.get("sku") if mapped else None,
                "daily_units": daily_units,
                "daily_retail": int(daily_retail)
                if daily_retail == daily_retail.to_integral()
                else float(daily_retail),
                "prior_zero_proven": new_row,
                "method": method,
            }
        )
    derived_units = sum(x["daily_units"] for x in rows)
    derived_retail = sum(Decimal(str(x["daily_retail"])) for x in rows)
    expected_units = current["grand"]["week_units"] - (
        0 if sunday else previous["grand"]["week_units"]
    )
    expected_retail = Decimal(str(current["grand"]["week_retail"])) - Decimal(
        str(0 if sunday else previous["grand"]["week_retail"])
    )
    if derived_units != expected_units or derived_retail != expected_retail:
        raise ReportError("Daily product changes do not reconcile to the grand-total change")
    return {
        "date": current["date"],
        "message_id": current.get("message_id", ""),
        "sheets_date_serial": (date - dt.date(1899, 12, 30)).days,
        "date_note": current.get("date_note", ""),
        "previous_message_id": previous.get("message_id", "") if previous and not sunday else None,
        "week_starts": sunday_start(date).isoformat(),
        "reported_daily_units": current["reported_daily_units"],
        "reported_daily_retail": current["reported_daily_retail"],
        "derived_daily_units": derived_units,
        "derived_daily_retail": int(derived_retail)
        if derived_retail == derived_retail.to_integral()
        else float(derived_retail),
        "units_variance": derived_units - current["reported_daily_units"],
        "products": rows,
        "grand": current["grand"],
        "unmapped_products": unmapped,
        "safe_to_write": not unmapped,
    }


def load_source(path):
    content = Path(path).read_text()
    return json.loads(content) if Path(path).suffix.lower() == ".json" else content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--message-id", default="")
    parser.add_argument("--previous")
    parser.add_argument("--previous-message-id", default="")
    parser.add_argument(
        "--sku-map", default=str(Path(__file__).with_name("target_product_map.json"))
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    current = parse_report(load_source(args.current), args.date, args.message_id)
    previous_date = (dt.date.fromisoformat(args.date) - dt.timedelta(days=1)).isoformat()
    previous = (
        parse_report(load_source(args.previous), previous_date, args.previous_message_id)
        if args.previous
        else None
    )
    result = derive_daily(current, previous, json.loads(Path(args.sku_map).read_text()))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ["products", "grand"]}))
    if not result["safe_to_write"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
