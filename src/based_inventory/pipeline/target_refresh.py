"""Read dated Target mail and publish independently verified retail rows."""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .cloud_helpers.collect_inventory_snapshot import cell
from .cloud_helpers.parse_target_report import ReportError, derive_daily, parse_report
from .cloud_helpers.verify_target_readback import verify
from .google_api import ERP_ID, GoogleAPIError

ASSETS = Path(__file__).with_name("cloud_helpers")
TARGET_TAB = 560223170


def validate_mode(mode):
    if mode not in {"compare", "publish"}:
        raise ValueError("Unknown source refresh mode")
    if mode == "publish" and os.getenv("PIPELINE_ALLOW_WRITES") != "1":
        raise PermissionError("Cloud source writes are disabled")


class SourceIncomplete(ValueError):  # noqa: N818 (public source contract)
    """Source coverage cannot support an authoritative update."""


def asset(name):
    return json.loads((ASSETS / name).read_text())


def plaintext_part(part, google, message_id):
    """Decode Gmail's URL-safe base64; never use snippets as reports."""
    if part.get("mimeType") == "text/plain":
        body = part.get("body", {})
        data = body.get("data")
        if data is None and body.get("attachmentId"):
            data = google.json(
                "GET",
                f'https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/attachments/{body["attachmentId"]}',
            ).get("data")
        if data is not None:
            try:
                return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8")
            except (ValueError, UnicodeError):
                raise SourceIncomplete("Target plaintext MIME encoding is invalid") from None
    return "\n".join(
        filter(None, (plaintext_part(p, google, message_id) for p in part.get("parts", [])))
    )


def collect_target(day: date, google, query: str):
    if not query.strip():
        raise SourceIncomplete("A verified Target sender/search query is required")
    listing = google.json(
        "GET",
        "https://gmail.googleapis.com/gmail/v1/users/me/messages",
        params={"q": query, "maxResults": 100},
    )
    # Refuse an incomplete inbox rather than picking the first visible report.
    if listing.get("nextPageToken"):
        raise SourceIncomplete("Target mail search is paginated; narrow the verified query")
    required = {day} if day.weekday() == 6 else {day, day - timedelta(days=1)}
    matches = {d: [] for d in required}
    seen = set()
    for ref in listing.get("messages", []):
        ident = ref["id"]
        if ident in seen:
            continue
        seen.add(ident)
        msg = google.json(
            "GET",
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{ident}",
            params={"format": "full"},
        )
        payload = msg.get("payload", {})
        text = plaintext_part(payload, google, ident)
        if not text:
            continue
        source = {
            "payload": {
                "mimeType": "text/plain",
                "headers": payload.get("headers", []),
                "body": {"content": text},
            }
        }
        for d in required:
            try:
                parsed = parse_report(source, d.isoformat(), ident)
            except ReportError:
                continue
            launch = re.search(r"([\d,]+)\s+Launch to date", text, re.I)
            stores = re.search(r"([\d,]+)\s+stores registered", text, re.I)
            parsed["workbook_summary"] = {
                "asp": parsed["grand"]["asp"],
                "overall_oos": parsed["grand"]["out_of_stock"],
            }
            if launch:
                parsed["workbook_summary"]["launch_units"] = int(launch.group(1).replace(",", ""))
            if stores:
                parsed["workbook_summary"]["stores"] = int(stores.group(1).replace(",", ""))
            if msg.get("internalDate"):
                received = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=UTC)
                parsed["workbook_summary"]["received_serial"] = (
                    received - datetime(1899, 12, 30, tzinfo=UTC)
                ).total_seconds() / 86400
            matches[d].append(parsed)
    for d, candidates in matches.items():
        if len(candidates) != 1:
            raise SourceIncomplete(
                f"Target {d}: "
                + (
                    "multiple matching reports require source review"
                    if candidates
                    else "complete dated report missing"
                )
            )
    result = derive_daily(
        matches[day][0],
        None if day.weekday() == 6 else matches[day - timedelta(days=1)][0],
        asset("target_product_map.json"),
    )
    result["workbook_summary"] = matches[day][0]["workbook_summary"]
    if not result["safe_to_write"]:
        raise SourceIncomplete("Target report contains unmapped products")
    return result


def target_rows(parsed):
    link = "https://mail.google.com/mail/u/0/#all/" + parsed["message_id"]
    return [
        [
            parsed["sheets_date_serial"],
            p["product"],
            p["sku"],
            p["daily_units"],
            p["daily_retail"],
            p["week_units"],
            p["week_retail"],
            p["week_forecast_units"],
            p["out_of_stock"],
            parsed["message_id"],
            link,
            p["method"]
            + f'; reported {parsed["reported_daily_units"]}; derived {parsed["derived_daily_units"]}; variance {parsed["units_variance"]}',
        ]
        for p in parsed["products"]
    ]


def target_plan(parsed, google):
    metadata = google.metadata(ERP_ID)
    tabs = [
        s["properties"]
        for s in metadata.get("sheets", [])
        if s["properties"]["sheetId"] == TARGET_TAB
    ]
    if len(tabs) != 1:
        raise SourceIncomplete("Target Daily tab identity missing")
    title = tabs[0]["title"]
    a1 = "'" + title.replace("'", "''") + "'"
    rows = google.values(ERP_ID, a1 + "!A:L")
    # Verify semantic column order before a write. Header labels are intentionally strict.
    expected = [
        "date",
        "product",
        "sku",
        "daily units",
        "daily retail sales",
        "week units",
        "week retail sales",
        "week forecast units",
        "out of stock %",
    ]
    headers = [i for i, r in enumerate(rows) if [str(v).strip().lower() for v in r[:9]] == expected]
    if len(headers) != 1:
        raise SourceIncomplete("Target Daily A:I header mapping is not verified")
    indices = [i for i, r in enumerate(rows) if r and r[0] == parsed["sheets_date_serial"]]
    data = target_rows(parsed)
    if indices:
        if indices != list(range(indices[0], indices[0] + len(data))):
            raise SourceIncomplete("Existing Target day has duplicate or changed product coverage")
        start = indices[0]
    else:
        start = len(rows)
    request = {
        "updateCells": {
            "start": {"sheetId": TARGET_TAB, "rowIndex": start, "columnIndex": 0},
            "rows": [{"values": [cell(v) for v in r]} for r in data],
            "fields": "userEnteredValue",
        }
    }
    return request, a1, f"{a1}!A{start+1}:L{start+len(data)}"


def publish_target(parsed, google, *, mode="compare", before_write=None):
    validate_mode(mode)
    request, a1, read_range = target_plan(parsed, google)
    summary, summary_a1, summary_index, old_summary = target_summary_plan(parsed, google)
    dynamic_requests, dynamic_a1, dynamic_expected = target_dynamic_plan(parsed, google)
    requests = [request, *summary]
    from .google_api import DYNAMIC_ID

    if mode == "publish" and before_write:
        before_write(
            {
                "requests": {ERP_ID: requests, DYNAMIC_ID: dynamic_requests},
                "target": parsed,
                "dynamic_expected": dynamic_expected,
                "dynamic_a1": dynamic_a1,
            }
        )
    if mode == "publish":
        from contextlib import suppress

        with suppress(GoogleAPIError):
            google.batch(ERP_ID, requests)
    from .google_api import DYNAMIC_ID

    if mode == "publish" and dynamic_requests:
        from contextlib import suppress

        with suppress(GoogleAPIError):
            google.batch(DYNAMIC_ID, dynamic_requests)
    verify_requests(google, DYNAMIC_ID, dynamic_requests)
    dynamic_actual = [
        r
        for r in google.values(DYNAMIC_ID, dynamic_a1 + "!M:Q")
        if r and r[0] == parsed["sheets_date_serial"]
    ]
    if sorted(dynamic_actual, key=lambda r: r[3]) != sorted(dynamic_expected, key=lambda r: r[3]):
        raise SourceIncomplete("Dynamic Target native projection differs from parsed daily source")
    verify_requests(google, ERP_ID, requests)
    summary_native = google.values(ERP_ID, f"{summary_a1}!A{summary_index+1}:J{summary_index+1}")
    if len(summary_native) != 1 or any(
        summary_native[0][c] != old_summary[c] for c in (0, 1, 2, 3, 4, 7)
    ):
        raise SourceIncomplete("Target summary changed stored merchant values or provenance")
    native = google.cells(ERP_ID, [read_range])
    native["spreadsheetId"] = ERP_ID
    result = verify(parsed, native)
    return {
        "status": "verified",
        "date": parsed["date"],
        "source_message_id": parsed["message_id"],
        "readback": result,
        "verification": {
            "requests": {ERP_ID: requests, DYNAMIC_ID: dynamic_requests},
            "target": parsed,
            "dynamic_expected": dynamic_expected,
            "dynamic_a1": dynamic_a1,
        },
        "rows": len(parsed["products"]),
    }


def verify_requests(google, spreadsheet, requests):
    """Verify every planned constant/note and table range from native CellData."""
    from openpyxl.utils.cell import get_column_letter

    tabs = {
        s["properties"]["sheetId"]: s["properties"]["title"]
        for s in google.metadata(spreadsheet).get("sheets", [])
    }
    for request in requests:
        if "updateTable" in request:
            table = request["updateTable"]["table"]
            native = google.json(
                "GET",
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet}",
                params={"fields": "sheets.tables"},
            )
            tables = [
                t
                for s in native.get("sheets", [])
                for t in s.get("tables", [])
                if t.get("tableId") == table["tableId"]
            ]
            if len(tables) != 1 or tables[0].get("range") != table["range"]:
                raise SourceIncomplete("Native source table range differs from verified plan")
            continue
        update = request["updateCells"]
        where = update.get("range", update.get("start"))
        row = where.get("startRowIndex", where.get("rowIndex", 0))
        col = where.get("startColumnIndex", where.get("columnIndex", 0))
        endrow = where.get("endRowIndex", row + len(update.get("rows", [])))
        endcol = where.get(
            "endColumnIndex", col + max(len(r.get("values", [])) for r in update["rows"])
        )
        title = "'" + tabs[where["sheetId"]].replace("'", "''") + "'"
        a1 = f"{title}!{get_column_letter(col+1)}{row+1}:{get_column_letter(endcol)}{endrow}"
        native = google.cells(spreadsheet, [a1])
        actual = {}
        for sheet in native.get("sheets", []):
            if sheet.get("properties", {}).get("sheetId") != where["sheetId"]:
                continue
            for grid in sheet.get("data", []):
                for r, cells in enumerate(grid.get("rowData", [])):
                    for c, value in enumerate(cells.get("values", [])):
                        actual[(grid.get("startRow", 0) + r, grid.get("startColumn", 0) + c)] = (
                            value
                        )
        for r in range(endrow - row):
            vals = (
                update.get("rows", [])[r].get("values", [])
                if r < len(update.get("rows", []))
                else []
            )
            for c in range(endcol - col):
                expected = vals[c] if c < len(vals) else {}
                got = actual.get((row + r, col + c), {})
                if "userEnteredValue" in update["fields"] and got.get(
                    "effectiveValue", {}
                ) != expected.get("userEnteredValue", {}):
                    raise SourceIncomplete("Native source cell differs from source-derived plan")
                if "note" in update["fields"] and got.get("note", "") != expected.get("note", ""):
                    raise SourceIncomplete("Native source stock note is stale")


def target_summary_plan(parsed, google):
    from .merchant_publish import DAILY_HEADER

    tabs = [
        s["properties"]
        for s in google.metadata(ERP_ID).get("sheets", [])
        if s["properties"]["sheetId"] == 1275479413
    ]
    if len(tabs) != 1:
        raise SourceIncomplete("ERP Daily Sales tab missing")
    a1 = "'" + tabs[0]["title"].replace("'", "''") + "'"
    existing = google.values(ERP_ID, a1 + "!A:J")
    if not existing or existing[0] != DAILY_HEADER:
        raise SourceIncomplete("ERP Daily Sales headers changed")
    indices = [i for i, r in enumerate(existing) if r and r[0] == parsed["sheets_date_serial"]]
    if len(indices) != 1:
        raise SourceIncomplete(
            "Stored merchant day must exist uniquely before Target summary update"
        )
    index = indices[0]
    old = existing[index] + [""] * (10 - len(existing[index]))
    marker = " Merchant verification: "
    if marker not in str(old[9]):
        raise SourceIncomplete(
            "ERP merchant version provenance is missing; Target cannot overwrite it"
        )
    provenance = marker + str(old[9]).split(marker, 1)[1]
    note = (
        f"Target reported {parsed['reported_daily_units']} daily retail units; complete product tables derive {parsed['derived_daily_units']} units (variance {parsed['units_variance']:+d}) and {parsed['derived_daily_retail']} retail sales. Source Gmail {parsed['message_id']}."
        + provenance
    )

    def update(col, values):
        return {
            "updateCells": {
                "start": {"sheetId": 1275479413, "rowIndex": index, "columnIndex": col},
                "rows": [{"values": [cell(v) for v in values]}],
                "fields": "userEnteredValue",
            }
        }

    return (
        [
            update(5, [parsed["reported_daily_units"], parsed["derived_daily_retail"]]),
            update(8, ["https://mail.google.com/mail/u/0/#all/" + parsed["message_id"], note]),
        ],
        a1,
        index,
        old,
    )


def target_dynamic_plan(parsed, google):
    from .google_api import DYNAMIC_ID

    tabs = [
        s["properties"]
        for s in google.metadata(DYNAMIC_ID).get("sheets", [])
        if s["properties"]["sheetId"] == 708272086
    ]
    if len(tabs) != 1:
        raise SourceIncomplete("Dynamic Target projection tab missing")
    a1 = "'" + tabs[0]["title"].replace("'", "''") + "'"
    existing = google.values(DYNAMIC_ID, a1 + "!M:Q")
    if not existing or existing[0] != ["Date", "Name", "Qty", "SKU", "Store"]:
        raise SourceIncomplete("Dynamic Target M:Q header contract changed")
    expected = [
        [parsed["sheets_date_serial"], p["product"], p["daily_units"], p["sku"], "TARGET"]
        for p in parsed["products"]
    ]
    current = [r for r in existing[1:] if r and r[0] == parsed["sheets_date_serial"]]
    if current:
        if sorted(current, key=lambda r: r[3]) != sorted(expected, key=lambda r: r[3]):
            raise SourceIncomplete(
                "Dynamic Target existing day disagrees with parsed source; historical formula rows are protected"
            )
        return [], a1, expected
    start = len(existing)
    end = start + len(expected)
    if end > tabs[0].get("gridProperties", {}).get("rowCount", 0):
        raise SourceIncomplete("Dynamic Target projection has no verified empty row capacity")
    a1range = f"{a1}!M{start+1}:Q{end}"
    native = google.cells(DYNAMIC_ID, [a1range])
    for sheet in native.get("sheets", []):
        for grid in sheet.get("data", []):
            for row in grid.get("rowData", []):
                if any(
                    c.get("userEnteredValue") or c.get("effectiveValue")
                    for c in row.get("values", [])
                ):
                    raise SourceIncomplete(
                        "Dynamic Target append cells contain existing formulas, spills or values"
                    )
    request = {
        "updateCells": {
            "start": {"sheetId": 708272086, "rowIndex": start, "columnIndex": 12},
            "rows": [{"values": [cell(v) for v in r]} for r in expected],
            "fields": "userEnteredValue",
        }
    }
    return [request], a1, expected
