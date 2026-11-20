"""Bounded Google API access with explicit tracker write boundaries."""

from __future__ import annotations

import time
from urllib.parse import quote

import requests

DYNAMIC_ID = "1vI9dM06MtptojGSxBAfNqOu0l0ZPyAGj0-D88ZF82eY"
ERP_ID = "1TE-gFZLELr1BYfaTkhaAqhSkvuid7a8X0cZthyA33I4"
WRITE_TABS = {
    DYNAMIC_ID: {
        2106291001,
        1740410291,
        1740410293,
        1740410294,
        708272086,
        1740410295,
        1740410296,
        1740410297,
    },
    ERP_ID: {1275479413, 1740410290, 560223170, 1740410298, 1740410299, 1740410300},
}


class GoogleAPIError(RuntimeError):
    """Redacted transport/authentication failure."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GoogleAPI:
    def __init__(self, client_id, client_secret, refresh_token, *, session=None, sleep=time.sleep):
        if not all((client_id, client_secret, refresh_token)):
            raise ValueError("Cloud Google OAuth credentials are required")
        self.credentials = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        }
        self.session = session or requests.Session()
        self.sleep = sleep
        self.token = None

    def _refresh(self):
        try:
            response = self.session.post(
                "https://oauth2.googleapis.com/token",
                data=self.credentials | {"grant_type": "refresh_token"},
                timeout=30,
            )
            if response.status_code != 200:
                raise GoogleAPIError("Google OAuth refresh rejected")
            token = response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise GoogleAPIError("Google OAuth response missing token")
            self.token = token
        except (requests.RequestException, ValueError):
            raise GoogleAPIError("Google OAuth refresh unavailable") from None

    def request(self, method, url, **kwargs):
        if not url.startswith(
            (
                "https://sheets.googleapis.com/",
                "https://gmail.googleapis.com/",
                "https://www.googleapis.com/drive/",
                "https://www.googleapis.com/upload/drive/",
            )
        ):
            raise ValueError("Unexpected Google API origin")
        refreshed = False
        headers = dict(kwargs.pop("headers", {}))
        for attempt in range(5):
            if not self.token:
                self._refresh()
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers | {"Authorization": "Bearer " + self.token},
                    timeout=60,
                    **kwargs,
                )
            except requests.RequestException:
                raise GoogleAPIError(
                    "Google API response unavailable; read back before retrying a write"
                ) from None
            if response.status_code == 401 and not refreshed:
                self._refresh()
                refreshed = True
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                self.sleep(min(2**attempt, 16))
                continue
            if not 200 <= response.status_code < 300:
                raise GoogleAPIError(
                    f"Google API HTTP {response.status_code}", response.status_code
                )
            return response
        raise GoogleAPIError("Google API retries exhausted")

    def json(self, method, url, **kwargs):
        try:
            return self.request(method, url, **kwargs).json()
        except ValueError:
            raise GoogleAPIError("Google API invalid JSON response") from None

    def metadata(self, spreadsheet):
        return self.json(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet, safe='')}",
            params={"fields": "spreadsheetId,properties,sheets(properties,tables)"},
        )

    def values(self, spreadsheet, a1):
        return self.json(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet, safe='')}/values/{quote(a1, safe='')}",
            params={
                "valueRenderOption": "UNFORMATTED_VALUE",
                "dateTimeRenderOption": "SERIAL_NUMBER",
            },
        ).get("values", [])

    def cells(self, spreadsheet, ranges):
        return self.json(
            "GET",
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet, safe='')}",
            params={
                "ranges": ranges,
                "includeGridData": "true",
                "fields": "sheets(properties,data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue,note,dataValidation))))",
            },
        )

    def batch(self, spreadsheet, requests_):
        if spreadsheet not in WRITE_TABS:
            raise ValueError("Spreadsheet is outside the write allowlist")
        for request in requests_:
            if set(request) == {"updateTable"}:
                update = request["updateTable"]
                table, bounds = update.get("table", {}), update.get("table", {}).get("range", {})
                expected = {
                    (ERP_ID, "1283118671"): (1740410290, 6, 23),
                    (DYNAMIC_ID, "1380483092"): (1740410294, 7, 17),
                }.get((spreadsheet, str(table.get("tableId"))))
                if not expected or update.get("fields") != "range":
                    raise ValueError("Unexpected native table update")
                sheet_id, first_row, columns = expected
                if (
                    bounds.get("sheetId") != sheet_id
                    or bounds.get("startRowIndex") != first_row
                    or bounds.get("startColumnIndex") != 0
                    or bounds.get("endColumnIndex") != columns
                    or not first_row < bounds.get("endRowIndex", 0) <= 10000
                ):
                    raise ValueError("Unexpected native table bounds")
                continue
            if set(request) != {"updateCells"}:
                raise ValueError("Only idempotent, row-scoped cell updates are allowed")
            update = request["updateCells"]
            location = update.get("start", update.get("range", {}))
            if spreadsheet == DYNAMIC_ID and location.get("sheetId") == 2106291003:
                raise ValueError("Canonical Sales Raw is formula-derived and read-only")
            if location.get("sheetId") not in WRITE_TABS[spreadsheet]:
                raise ValueError("Sheet tab is outside the write allowlist")
            if update.get("fields") not in {"userEnteredValue", "note", "userEnteredValue,note"}:
                raise ValueError("Unexpected cell write field mask")
        if not requests_:
            return {}
        self._preflight_values(spreadsheet, requests_)
        return self.json(
            "POST",
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet}:batchUpdate",
            json={"requests": requests_},
        )

    def _preflight_values(self, spreadsheet, requests_):
        assigned, bounds = set(), {}
        for request in requests_:
            update = request.get("updateCells")
            if not update or "userEnteredValue" not in update["fields"]:
                continue
            point = update.get("start", update.get("range"))
            sheet = point["sheetId"]
            row0 = point.get("rowIndex", point.get("startRowIndex", 0))
            col0 = point.get("columnIndex", point.get("startColumnIndex", 0))
            if "range" in update:
                if "endRowIndex" not in point or "endColumnIndex" not in point:
                    raise ValueError("Range writes must have explicit end bounds")
                end_row, end_col = point["endRowIndex"], point["endColumnIndex"]
                if not (0 <= row0 < end_row <= 100000 and 0 <= col0 < end_col <= 100):
                    raise ValueError("Unexpected source write range")
                positions = ((r, c) for r in range(row0, end_row) for c in range(col0, end_col))
            else:
                positions = (
                    (row0 + ri, col0 + ci)
                    for ri, row in enumerate(update.get("rows", []))
                    for ci, _ in enumerate(row.get("values", []))
                )
            for row, col in positions:
                target = (sheet, row, col)
                if spreadsheet == ERP_ID and sheet == 1740410290 and 8 <= col < 16:
                    raise ValueError("Inventory merchant formula columns are read-only")
                if spreadsheet == DYNAMIC_ID and sheet == 708272086 and not 12 <= col < 17:
                    raise ValueError("Only the Target daily projection M:Q may be updated")
                assigned.add(target)
                old = bounds.get(sheet, (row, row, col, col))
                bounds[sheet] = (
                    min(old[0], row),
                    max(old[1], row),
                    min(old[2], col),
                    max(old[3], col),
                )
        if not assigned:
            return
        titles = {
            s["properties"]["sheetId"]: s["properties"]["title"]
            for s in self.metadata(spreadsheet)["sheets"]
        }

        def column(index):
            value = ""
            index += 1
            while index:
                index, remainder = divmod(index - 1, 26)
                value = chr(65 + remainder) + value
            return value

        ranges = [
            f"'{titles[sheet].replace(chr(39), chr(39)*2)}'!{column(c0)}{r0+1}:{column(c1)}{r1+1}"
            for sheet, (r0, r1, c0, c1) in bounds.items()
        ]
        response = self.cells(spreadsheet, ranges)
        returned = {s["properties"]["sheetId"] for s in response.get("sheets", [])}
        if not set(bounds) <= returned:
            raise ValueError("Native formula preflight omitted a destination sheet")
        for sheet in response.get("sheets", []):
            sheet_id = sheet["properties"]["sheetId"]
            for data in sheet.get("data", []):
                for ri, row in enumerate(data.get("rowData", [])):
                    for ci, value in enumerate(row.get("values", [])):
                        target = (
                            sheet_id,
                            data.get("startRow", 0) + ri,
                            data.get("startColumn", 0) + ci,
                        )
                        if target not in assigned:
                            continue
                        if value.get("userEnteredValue", {}).get("formulaValue"):
                            raise ValueError("Source write would replace an existing formula")
                        if value.get("dataValidation"):
                            raise ValueError(
                                "Source write encountered unexpected native validation"
                            )

    def _platform_properties(self, spreadsheet, tab_id, title):
        from .platform_trackers import MAX_COLUMNS, MAX_ROWS, TABS

        if not any((spreadsheet, tab_id, title) == tab[:3] for tab in TABS):
            raise ValueError("Platform tab is outside the write allowlist")
        matches = [
            sheet["properties"]
            for sheet in self.metadata(spreadsheet)["sheets"]
            if sheet["properties"]["sheetId"] == tab_id or sheet["properties"]["title"] == title
        ]
        if not matches:
            return None
        if len(matches) != 1 or matches[0]["sheetId"] != tab_id or matches[0]["title"] != title:
            raise ValueError("Platform sales tab identity differs")
        props = matches[0]
        if props.get("sheetType", "GRID") != "GRID" or props.get("hidden", False):
            raise ValueError("Platform sales tab is hidden or has an unexpected type")
        if (
            props["gridProperties"]["rowCount"] != MAX_ROWS
            or props["gridProperties"]["columnCount"] != MAX_COLUMNS
        ):
            raise ValueError("Platform sales tab bounds changed")
        return props

    def platform_read(self, spreadsheet, tab_id, title):
        from .platform_trackers import MAX_ROWS

        if self._platform_properties(spreadsheet, tab_id, title) is None:
            return None
        return self.values(spreadsheet, f"'{title}'!A1:J{MAX_ROWS}")

    def platform_write(self, spreadsheet, tab_id, title, rows):
        from .platform_trackers import MAX_COLUMNS, MAX_ROWS

        if not rows or len(rows) > MAX_ROWS or any(len(row) > MAX_COLUMNS for row in rows):
            raise ValueError("Platform sales view exceeds safe bounds")
        if self._platform_properties(spreadsheet, tab_id, title) is None:
            # Only these six new tabs may be created; owner sheets cannot be renamed or resized.
            self.json(
                "POST",
                f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet}:batchUpdate",
                json={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "sheetId": tab_id,
                                    "title": title,
                                    "gridProperties": {
                                        "rowCount": MAX_ROWS,
                                        "columnCount": MAX_COLUMNS,
                                        "frozenRowCount": 13 if title == "Daily SKU Sales" else 8,
                                        "hideGridlines": True,
                                    },
                                }
                            }
                        },
                        {
                            "repeatCell": {
                                "range": {"sheetId": tab_id},
                                "cell": {
                                    "userEnteredFormat": {
                                        "wrapStrategy": "WRAP",
                                        "verticalAlignment": "TOP",
                                    }
                                },
                                "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
                            }
                        },
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": tab_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": 0,
                                    "endIndex": MAX_COLUMNS,
                                },
                                "properties": {"pixelSize": 220},
                                "fields": "pixelSize",
                            }
                        },
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": tab_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": 1,
                                    "endIndex": 2,
                                },
                                "properties": {
                                    "pixelSize": 1000 if title == "Daily Sales Methods" else 420
                                },
                                "fields": "pixelSize",
                            }
                        },
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": tab_id,
                                    "dimension": "ROWS",
                                    "startIndex": 0,
                                    "endIndex": len(rows),
                                },
                                "properties": {
                                    "pixelSize": 150 if title == "Daily Sales Methods" else 90
                                },
                                "fields": "pixelSize",
                            }
                        },
                    ]
                },
            )
            if self.platform_read(spreadsheet, tab_id, title) not in ([], None):
                raise ValueError("New platform sales tab contains unexpected values")

        def cell(value):
            if value in (None, ""):
                return {}
            key = (
                "boolValue"
                if isinstance(value, bool)
                else "numberValue"
                if isinstance(value, int | float)
                else "stringValue"
            )
            return {"userEnteredValue": {key: value}}

        # An explicit range clears stale trailing values within the dedicated view only.
        self.batch(
            spreadsheet,
            [
                {
                    "updateCells": {
                        "range": {
                            "sheetId": tab_id,
                            "startRowIndex": 0,
                            "endRowIndex": MAX_ROWS,
                            "startColumnIndex": 0,
                            "endColumnIndex": MAX_COLUMNS,
                        },
                        "rows": [{"values": [cell(value) for value in row]} for row in rows],
                        "fields": "userEnteredValue",
                    }
                }
            ],
        )
