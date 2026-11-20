"""Update a configured cloud XLSX in place, preserving untouched ZIP members."""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from hashlib import sha256
from io import BytesIO
from urllib.parse import quote

DRIVE = "0ANDGo4RyWVY2Uk9PVA"
FOLDER = "1Fw8-tnitkJKllxKA2ysAgvaGibWFYDxL"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def verify_destination(meta):
    if meta.get("driveId") != DRIVE or FOLDER not in meta.get("parents", []):
        raise ValueError("Target workbook must already be in Marketing Operations shared drive")
    if meta.get("trashed") or meta.get("mimeType", XLSX) != XLSX:
        raise ValueError("Target workbook must be an existing XLSX")


def metadata(google, file_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id or ""):
        raise ValueError("A configured existing cloud Target workbook file ID is required")
    result = google.json(
        "GET",
        f'https://www.googleapis.com/drive/v3/files/{quote(file_id,safe="")}',
        params={
            "supportsAllDrives": "true",
            "fields": "id,driveId,parents,mimeType,trashed,version,md5Checksum",
        },
    )
    verify_destination(result)
    return result


def patch_cells(content: bytes, sheet_name: str, changes: dict[str, object]) -> bytes:
    """Patch only explicit constant cells; formulas and other ZIP members survive."""
    if not changes:
        return content
    with zipfile.ZipFile(BytesIO(content)) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        matches = [
            s for s in workbook.findall("s:sheets/s:sheet", NS) if s.get("name") == sheet_name
        ]
        if len(matches) != 1:
            raise ValueError("Configured Target workbook sheet not found")
        rid = matches[0].get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = [r.get("Target") for r in rels if r.get("Id") == rid]
        if len(targets) != 1:
            raise ValueError("Workbook sheet relationship missing")
        path = (
            targets[0].lstrip("/")
            if targets[0].startswith("/")
            else posixpath.normpath("xl/" + targets[0])
        )
        # Byte-level cell replacement preserves namespace prefixes, formula caches,
        # extension lists and every other pre-existing cell exactly.
        xml = archive.read(path).decode("utf-8")
        from xml.sax.saxutils import escape

        for address, value in changes.items():
            if not re.fullmatch("[A-Z]+[1-9][0-9]*", address):
                raise ValueError("Invalid workbook cell address")
            pattern = re.compile(r'<c\b(?=[^>]*\br="' + address + r'")[^>]*?(?:/>|>.*?</c>)', re.S)
            found = list(pattern.finditer(xml))
            if len(found) != 1:
                raise ValueError(
                    "Workbook update requires an existing uniquely identified cell: " + address
                )
            old = found[0].group()
            if re.search(r"<(?:\w+:)?f(?:\s|>)", old):
                raise ValueError("Workbook formula cell is protected: " + address)
            if value is None:
                new = f'<c r="{address}"/>'
            elif isinstance(value, int | float) and not isinstance(value, bool):
                import math

                if not math.isfinite(value):
                    raise ValueError("Invalid workbook numeric value")
                new = f'<c r="{address}"><v>{value}</v></c>'
            else:
                new = f'<c r="{address}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
            # Preserve formatting attributes from the original constant cell.
            style = re.search(r'\bs="[^"]*"', old.split(">")[0])
            if style:
                new = new.replace(" r=", " " + style.group() + " r=", 1)
            xml = xml[: found[0].start()] + new + xml[found[0].end() :]
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as dest:
            for member in archive.infolist():
                dest.writestr(
                    member,
                    xml.encode() if member.filename == path else archive.read(member.filename),
                )
    return output.getvalue()


def target_workbook_update(content, parsed):
    """Use the inspected Daily Data layout; reserve a preformatted empty row.

    Historical rows and formula expressions are retained byte for byte. Daily,
    Summary and chart caches are calculated for the validated template.
    """
    from datetime import date, datetime, timedelta

    from openpyxl import load_workbook

    book = load_workbook(BytesIO(content), read_only=True, data_only=False)
    if "Daily Data" not in book.sheetnames:
        raise ValueError("Target workbook Daily Data layout is missing")
    ws = book["Daily Data"]
    expected = [
        "Report date",
        "Week end",
        "Days covered",
        "Daily units reported",
        "Retail sales increment",
        "Units increment",
        "Forecast units increment",
        "Retail ASP",
        "WTD retail sales",
        "WTD units",
        "WTD forecast units",
        "WTD unit variance",
        "Launch-to-date units",
        "Stores with sale",
        "Overall OOS",
        "Sea Salt OOS",
        "Leave-In OOS",
        "Texture Powder OOS",
        "Curl Cream OOS",
        "Email received",
        "Data note",
        "Email source",
        "Message ID",
    ]
    if [ws.cell(4, c).value for c in range(1, 24)] != expected:
        raise ValueError("Target workbook Daily Data headers changed")
    day = date.fromisoformat(parsed["date"])
    populated = []
    matches = []
    blanks = []
    for row in ws.iter_rows(min_row=5, max_row=500, max_col=23):
        n = row[0].row
        value = row[0].value
        if value is None:
            blanks.append(n)
            continue
        if not isinstance(value, datetime):
            raise ValueError("Target workbook has a non-date reporting row")
        populated.append((n, value.date()))
        if value.date() == day:
            matches.append(n)
    if len(matches) > 1:
        raise ValueError("Target workbook contains duplicate reporting dates")
    if any(d > day for _, d in populated):
        raise ValueError(
            "Target workbook contains later reporting dates; historical repair needs full recalculation"
        )
    row = matches[0] if matches else (blanks[0] if blanks else None)
    if row is None:
        raise ValueError("Target workbook preformatted row capacity exceeded")
    if populated and not matches and populated[-1][1] != day - timedelta(days=1):
        raise ValueError("Target workbook is missing intervening reporting dates")
    serial = parsed["sheets_date_serial"]
    products = parsed["products"]
    forecast = sum(p["week_forecast_units"] for p in products)
    oos = {p["product"]: p["out_of_stock"] for p in products}
    values = {
        "A": serial,
        "D": parsed["reported_daily_units"],
        "I": sum(p["week_retail"] for p in products),
        "J": sum(p["week_units"] for p in products),
        "K": forecast,
        "P": oos.get("Sea Salt Spray"),
        "Q": oos.get("Leave-In Conditioner"),
        "R": oos.get("Texture Powder"),
        "S": oos.get("Curl Cream"),
        "U": f"Parsed {len(products)} products. Reported {parsed['reported_daily_units']} units; derived {parsed['derived_daily_units']}; variance {parsed['units_variance']}. Product forecast {forecast}; grand forecast {parsed['grand']['week_forecast_units']}. Prior Gmail {parsed.get('previous_message_id') or 'Sunday boundary'}. Additional product stockout rates retained in native Target Daily.",
        "V": "https://mail.google.com/mail/u/0/#all/" + parsed["message_id"],
        "W": parsed["message_id"],
    }
    extras = parsed.get("workbook_summary", {})
    if any(
        extras.get(field) is None
        for field in ("asp", "launch_units", "stores", "overall_oos", "received_serial")
    ):
        raise ValueError(
            "Target source lacks complete workbook headline, ASP, stockout or reception evidence"
        )
    for field, col in [
        ("asp", "H"),
        ("launch_units", "M"),
        ("stores", "N"),
        ("overall_oos", "O"),
        ("received_serial", "T"),
    ]:
        if field in extras:
            values[col] = extras[field]
    changes = {col + str(row): value for col, value in values.items()}
    updated = patch_cells(content, "Daily Data", changes)
    # The known formulas are validated before touching their cached values.
    expected_formulas = {
        "B": f'IF($A{row}="","",$A{row}+MOD(6-WEEKDAY($A{row},2),7))',
        "C": f'IF($A{row}="","",MAX(1,$A{row}-$A{row-1}))',
        "E": f'IF($A{row}="","",$I{row}-IF($B{row}=$B{row-1},$I{row-1},0))',
        "F": f'IF($A{row}="","",$J{row}-IF($B{row}=$B{row-1},$J{row-1},0))',
        "G": f'IF($A{row}="","",$K{row}-IF($B{row}=$B{row-1},$K{row-1},0))',
        "L": f'IF($A{row}="","",$J{row}/$K{row}-1)',
    }
    cached = load_workbook(BytesIO(content), read_only=True, data_only=True)["Daily Data"]
    previous_date = cached.cell(row - 1, 1).value
    week_end = serial + (5 - day.weekday()) % 7
    previous_week = cached.cell(row - 1, 2).value
    if isinstance(previous_week, datetime):
        previous_week = (previous_week.date() - date(1899, 12, 30)).days
    previous_forecast = cached.cell(row - 1, 11).value if previous_week == week_end else 0
    if previous_forecast is None:
        raise ValueError("Prior Target forecast cache is missing")
    caches = {
        "B": week_end,
        "C": max(1, (day - previous_date.date()).days)
        if isinstance(previous_date, datetime)
        else 1,
        "E": parsed["derived_daily_retail"],
        "F": parsed["derived_daily_units"],
        "G": forecast - previous_forecast,
        "L": values["J"] / forecast - 1 if forecast else None,
    }
    if caches["L"] is None:
        raise ValueError("Zero Target forecast cannot populate WTD variance formula")
    with zipfile.ZipFile(BytesIO(updated)) as archive:
        # The current template's Daily Data is sheet2. Verify relationship rather
        # than trusting that the name always means a fixed XML member.
        root = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet = next(
            s for s in root.findall("s:sheets/s:sheet", NS) if s.get("name") == "Daily Data"
        )
        rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels if r.get("Id") == rid)
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
        text = archive.read(path).decode()
        for col, value in caches.items():
            if ws.cell(row, ord(col) - 64).value != "=" + expected_formulas[col]:
                raise ValueError("Target workbook formula contract changed at " + col + str(row))
            pattern = re.compile(
                r'(<c\b(?=[^>]*\br="' + col + str(row) + r'")[^>]*>)(.*?)(</c>)', re.S
            )

            def cache(match, value=value):
                opening = re.sub(r'\s+t="[^"]*"', "", match.group(1))
                body, count = re.subn(
                    r"<v\b[^>]*?(?:/>|>.*?</v>)",
                    f"<v>{value}</v>",
                    match.group(2),
                    count=1,
                    flags=re.S,
                )
                if not count:
                    body += f"<v>{value}</v>"
                return opening + body + match.group(3)

            text, count = pattern.subn(cache, text)
            if count != 1:
                raise ValueError("Target formula cache cell missing")
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as dest:
            for info in archive.infolist():
                dest.writestr(
                    info, text.encode() if info.filename == path else archive.read(info.filename)
                )
    saved = refresh_summary_caches(output.getvalue())
    native = load_workbook(BytesIO(saved), data_only=True, read_only=True)["Daily Data"]
    for col, value in values.items():
        actual = native.cell(row, ord(col) - 64).value
        if isinstance(actual, datetime):
            actual = (actual - datetime(1899, 12, 30)).total_seconds() / 86400
        if actual != value:
            raise ValueError("Cloud workbook source readback mismatch at " + col + str(row))
    return saved


def archive_target_workbook(google, file_id, parsed, *, mode="compare", before_write=None):
    from .target_refresh import validate_mode

    validate_mode(mode)
    before = metadata(google, file_id)
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    original = google.request(
        "GET", url, params={"alt": "media", "supportsAllDrives": "true"}
    ).content
    updated = target_workbook_update(original, parsed)
    if mode == "publish" and before_write:
        import base64

        before_write(
            {
                "file_id": file_id,
                "file_digest": sha256(updated).hexdigest(),
                "original_digest": sha256(original).hexdigest(),
                "updated_base64": base64.b64encode(updated).decode(),
            }
        )
    if mode == "publish" and updated != original:
        if metadata(google, file_id).get("version") != before.get("version"):
            raise ValueError("Cloud Target workbook changed during capture")
        google.request(
            "PATCH",
            f"https://www.googleapis.com/upload/drive/v3/files/{file_id}",
            params={"uploadType": "media", "supportsAllDrives": "true"},
            headers={"Content-Type": XLSX},
            data=updated,
        )
    saved = google.request("GET", url, params={"alt": "media", "supportsAllDrives": "true"}).content
    metadata(google, file_id)
    if sha256(saved).hexdigest() != sha256(updated).hexdigest():
        raise ValueError("Cloud Target workbook uploaded bytes differ from verified source update")
    return {
        "status": "verified",
        "file_id": file_id,
        "digest": sha256(saved).hexdigest(),
        "date": parsed["date"],
        "summary_caches": "recalculated and verified",
        "verification": {"file_id": file_id, "file_digest": sha256(saved).hexdigest()},
    }


def refresh_summary_caches(content):
    """Calculate only the inspected template formulas and its monthly chart."""
    from datetime import datetime

    from openpyxl import load_workbook

    formulas = load_workbook(BytesIO(content), data_only=False, read_only=True)
    cached = load_workbook(BytesIO(content), data_only=True, read_only=True)
    if "Summary" not in formulas.sheetnames:
        raise ValueError("Target Summary sheet missing")
    summary = formulas["Summary"]
    daily = cached["Daily Data"]
    rows = [
        r for r in daily.iter_rows(min_row=5, max_row=500, max_col=23) if r[0].value is not None
    ]
    if not rows:
        raise ValueError("Target workbook contains no reports")
    values = [[c.value for c in r] for r in rows]
    if any(not isinstance(r[0], datetime) for r in values):
        raise ValueError("Target history dates are invalid")
    latest = max(values, key=lambda r: r[0])

    def number(value):
        if isinstance(value, datetime):
            return (value - datetime(1899, 12, 30)).total_seconds() / 86400
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
        if value is None:
            return 0
        raise ValueError("Target cached source contains nonnumeric totals")

    def total(col):
        return sum(number(r[col]) for r in values)

    expected = {
        "A7": "SUM('Daily Data'!$E$5:$E$500)",
        "C7": "SUM('Daily Data'!$F$5:$F$500)",
        "E7": "SUM('Daily Data'!$G$5:$G$500)",
        "G7": "C7/E7-1",
        "I7": "INDEX('Daily Data'!$M$5:$M$500,MATCH(MAX('Daily Data'!$A$5:$A$500),'Daily Data'!$A$5:$A$500,0))",
        "K7": "INDEX('Daily Data'!$O$5:$O$500,MATCH(MAX('Daily Data'!$A$5:$A$500),'Daily Data'!$A$5:$A$500,0))",
        "B35": "MAX('Daily Data'!$A$5:$A$500)",
        "B36": "COUNT('Daily Data'!$A$5:$A$500)",
        "B37": "INDEX('Daily Data'!$T$5:$T$500,MATCH(MAX('Daily Data'!$A$5:$A$500),'Daily Data'!$A$5:$A$500,0))",
    }
    if not total(6):
        raise ValueError("Target forecast total is zero")
    cache = {
        "A7": total(4),
        "C7": total(5),
        "E7": total(6),
        "G7": total(5) / total(6) - 1,
        "I7": number(latest[12]),
        "K7": number(latest[14]),
        "B35": number(latest[0]),
        "B36": len(values),
        "B37": number(latest[19]),
    }
    chart_dates = []
    chart_values = []
    last_month = None
    for n in range(13, 31):
        month = summary.cell(n, 1).value
        if not isinstance(month, datetime):
            raise ValueError("Target monthly Summary date layout changed")
        cohort = [r for r in values if (r[0].year, r[0].month) == (month.year, month.month)]
        expected[f"B{n}"] = (
            f"IF(COUNTIFS('Daily Data'!$A$5:$A$500,\">=\"&$A{n},'Daily Data'!$A$5:$A$500,\"<\"&EDATE($A{n},1))=0,\"\",SUMIFS('Daily Data'!$E$5:$E$500,'Daily Data'!$A$5:$A$500,\">=\"&$A{n},'Daily Data'!$A$5:$A$500,\"<\"&EDATE($A{n},1)))"
        )
        for col, source in [("C", "F"), ("D", "G")]:
            expected[f"{col}{n}"] = (
                f'IF(B{n}="","",SUMIFS(\'Daily Data\'!${source}$5:${source}$500,\'Daily Data\'!$A$5:$A$500,">="&$A{n},\'Daily Data\'!$A$5:$A$500,"<"&EDATE($A{n},1)))'
            )
        expected[f"E{n}"] = f'IF(B{n}="","",C{n}/D{n}-1)'
        totals = [sum(number(r[c]) for r in cohort) for c in (4, 5, 6)]
        if cohort and not totals[2]:
            raise ValueError("Target monthly forecast is zero")
        for col, value in zip(
            "BCDE", [*totals, totals[1] / totals[2] - 1 if cohort else None], strict=True
        ):
            cache[f"{col}{n}"] = value if cohort else None
        if cohort:
            last_month = n
        chart_dates.append(number(month))
        chart_values.append(totals[0])
    actual = {
        c.coordinate: c.value[1:]
        for r in summary.iter_rows(min_row=1, max_row=100, max_col=20)
        for c in r
        if isinstance(c.value, str) and c.value.startswith("=")
    }
    if actual != expected:
        raise ValueError("Unsupported Target Summary formula; cached recalculation refused")
    if last_month is None:
        raise ValueError("Target monthly chart has no source dates")
    chart_dates = chart_dates[: last_month - 12]
    chart_values = chart_values[: last_month - 12]
    with zipfile.ZipFile(BytesIO(content)) as archive:
        # Both known sheet names and relationship targets were inspected. Require
        # the same template member identities before updating cached formulas.
        text = archive.read("xl/worksheets/sheet1.xml").decode()
        for address, value in cache.items():
            pattern = re.compile(r'(<c\b(?=[^>]*\br="' + address + r'")[^>]*>)(.*?)(</c>)', re.S)

            def replace(match, value=value):
                opening = re.sub(r'\s+t="[^"]*"', "", match.group(1))
                if value is None:
                    opening = opening[:-1] + ' t="str">'
                body = re.sub(r"<v\b[^>]*?(?:/>|>.*?</v>)", "", match.group(2), flags=re.S)
                return (
                    opening
                    + body
                    + "<v>"
                    + ("" if value is None else str(value))
                    + "</v>"
                    + match.group(3)
                )

            text, count = pattern.subn(replace, text)
            if count != 1:
                raise ValueError("Target Summary cache cell missing: " + address)
        chart_members = [
            n
            for n in archive.namelist()
            if n.endswith(".xml") and re.search(r"/charts/chart\d+\.xml$", n)
        ]
        if len(chart_members) != 1:
            raise ValueError("Unsupported Target chart layout")
        chart_path = chart_members[0]
        chart = archive.read(chart_path).decode()
        for column, points in [("A", chart_dates), ("B", chart_values)]:
            pat = re.compile(
                r"(<c:numRef>\s*)<c:f>Summary!\$"
                + column
                + r"\$13:\$"
                + column
                + r"\$\d+</c:f>\s*<c:numCache>(.*?)</c:numCache>(\s*</c:numRef>)",
                re.S,
            )

            def chart_cache(match, column=column, points=points):
                fmt = re.search(r"<c:formatCode>.*?</c:formatCode>", match.group(2), re.S)
                if fmt is None:
                    raise ValueError("Target chart format missing")
                pts = "".join(
                    f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in enumerate(points)
                )
                return (
                    match.group(1)
                    + f"<c:f>Summary!${column}$13:${column}${last_month}</c:f><c:numCache>"
                    + fmt.group()
                    + f'<c:ptCount val="{len(points)}"/>'
                    + pts
                    + "</c:numCache>"
                    + match.group(3)
                )

            chart, count = pat.subn(chart_cache, chart)
            if count != 1:
                raise ValueError("Target chart source reference changed")
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as dest:
            for info in archive.infolist():
                dest.writestr(
                    info,
                    text.encode()
                    if info.filename == "xl/worksheets/sheet1.xml"
                    else chart.encode()
                    if info.filename == chart_path
                    else archive.read(info.filename),
                )
    return output.getvalue()
