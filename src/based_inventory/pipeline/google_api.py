"""Bounded Google API access with explicit tracker write boundaries."""

from __future__ import annotations

import time
from urllib.parse import quote

import requests

DYNAMIC_ID = "1vI9dM06MtptojGSxBAfNqOu0l0ZPyAGj0-D88ZF82eY"
ERP_ID = "1TE-gFZLELr1BYfaTkhaAqhSkvuid7a8X0cZthyA33I4"
WRITE_TABS = {
    DYNAMIC_ID: {2106291001, 1740410291, 1740410293, 1740410294, 708272086},
    ERP_ID: {1275479413, 1740410290, 560223170},
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
