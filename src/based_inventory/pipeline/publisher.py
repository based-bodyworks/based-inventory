"""Apply frozen merchant plans and prove native results before recording success."""

from __future__ import annotations

import math
import time
from contextlib import suppress

from .google_api import DYNAMIC_ID, ERP_ID, GoogleAPIError
from .merchant_publish import build_merchant_plan, equal_rows, pad, serial, validate_plan_readback
from .models import CHANNELS, canonical, digest

TABS = {
    "history": (DYNAMIC_ID, 2106291001, "Verified Sales History", "H"),
    "rolling": (DYNAMIC_ID, 1740410291, "Rolling7Demand", "T"),
    "daily": (ERP_ID, 1275479413, "Daily Sales", "J"),
    "map": (DYNAMIC_ID, 2106291005, "S&OP Product Map", "C"),
    "inventory": (ERP_ID, 1740410290, "Current Inventory", "AJ"),
}


def merchant_daily(rows):
    return [[pad(r, 10)[i] for i in (0, 1, 2, 3, 4, 7)] for r in rows]


class MerchantPublisher:
    def __init__(self, store, google, *, sleep=time.sleep):
        self.store, self.google, self.sleep = store, google, sleep
        self.limits = {}

    def collect(self):
        metadata = {file: self.google.metadata(file) for file in (DYNAMIC_ID, ERP_ID)}
        result = {}
        for key, (file, tab_id, title, end_column) in TABS.items():
            matches = [
                s["properties"]
                for s in metadata[file]["sheets"]
                if s["properties"]["sheetId"] == tab_id
            ]
            if len(matches) != 1 or matches[0]["title"] != title:
                raise ValueError(f"Missing or renamed {key} sheet")
            limit = matches[0]["gridProperties"]["rowCount"]
            self.limits[key] = limit
            result[key] = self.google.values(file, f"'{title}'!A1:{end_column}{limit}")
        if not result["map"] or result["map"][0][:3] != ["SKU", "Product", "Units per line"]:
            raise ValueError("Product map header differs from approved schema")
        result["map"] = result["map"][1:]
        cells = self.google.cells(
            DYNAMIC_ID, [f"'Verified Sales History'!C2:C{max(2, len(result['history']))}"]
        )
        notes = {}
        for sheet in cells.get("sheets", []):
            for data in sheet.get("data", []):
                for offset, row in enumerate(data.get("rowData", [])):
                    for cell in row.get("values", []):
                        if cell.get("dataValidation"):
                            raise ValueError("Sales quantity cells have unexpected validation")
                        if cell.get("note"):
                            notes[f"C{data.get('startRow', 0)+offset+1}"] = cell["note"]
        result["notes"] = notes
        return result

    def verify_canonical(self, version):
        metadata = self.google.metadata(DYNAMIC_ID)
        props = next(
            s["properties"] for s in metadata["sheets"] if s["properties"]["sheetId"] == 2106291003
        )
        if props["title"] != "Canonical Sales Raw":
            raise ValueError("Canonical formula tab identity differs")
        dates = self.google.values(
            DYNAMIC_ID, f"'Canonical Sales Raw'!B1:B{props['gridProperties']['rowCount']}"
        )
        target = serial(version.day)
        selected = [index + 1 for index, r in enumerate(dates) if r and r[0] == target]
        expected = {(r.sku, r.channel): r.units for r in version.rows}
        if not selected:
            if expected:
                raise ValueError("Canonical sales formula readback is incomplete")
            return
        rows = self.google.values(
            DYNAMIC_ID, f"'Canonical Sales Raw'!A{min(selected)}:F{max(selected)}"
        )
        actual = {}
        for row in rows:
            if len(row) < 6 or row[1] != target or row[5] not in CHANNELS:
                continue
            key = (str(row[4]), row[5])
            if key in actual:
                raise ValueError("Duplicate canonical sales readback")
            actual[key] = row[3]
        if any(key not in actual or actual[key] != value for key, value in expected.items()) or any(
            value != expected.get(key, 0) for key, value in actual.items()
        ):
            raise ValueError("Canonical sales formula readback differs from saved report")

    def verify_inventory(self, rolling):
        rows = self.google.values(ERP_ID, "'Current Inventory'!A1:AJ1000")
        items = {
            str(row[0]): (index, pad(row, 36))
            for index, row in enumerate(rows[7:], 7)
            if row and row[0]
        }
        cells = self.google.cells(ERP_ID, [f"'Current Inventory'!I8:P{max(8, len(rows))}"])
        formulas = {}
        for sheet in cells.get("sheets", []):
            for data in sheet.get("data", []):
                for offset, row in enumerate(data.get("rowData", [])):
                    formulas[data.get("startRow", 0) + offset] = row.get("values", [])
        for product in rolling[7:]:
            if not product or not product[0]:
                continue
            if str(product[1]) not in items:
                raise ValueError("Rolling product has no current warehouse row")
            index, row = items[str(product[1])]
            formula_cells = formulas.get(index, [])
            if len(formula_cells) != 8 or any(
                not c.get("userEnteredValue", {}).get("formulaValue")
                or c.get("effectiveValue", {}).get("errorValue")
                for c in formula_cells
            ):
                raise ValueError("Current Inventory merchant formulas are missing or invalid")
            if not equal_rows(
                [[row[8], row[9], row[13], row[14]]],
                [[product[5], product[6], product[7], product[8]]],
            ):
                raise ValueError("Inventory demand readback differs from saved rolling source")
            if type(row[4]) not in (int, float) or type(row[3]) not in (int, float):
                raise ValueError("Warehouse stock values are unverified")
            if product[6] == 0:
                if row[10] != "No observed demand" or row[11] != "No observed demand":
                    raise ValueError("Zero-demand cover readback differs")
            else:
                expected = (row[4] / product[6], row[4] / product[6] / 7, row[3] / product[6] / 7)
                if any(
                    type(a) not in (int, float)
                    or not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-8)
                    for a, b in zip(row[10:13], expected, strict=True)
                ):
                    raise ValueError(
                        "Stock cover readback differs from available stock and verified demand"
                    )
            if row[15] != "Complete":
                raise ValueError("Current Inventory source is stale or incomplete")

    def publish(self, version, source_url):
        self.store.assert_current(version.version_id)
        current = self.collect()
        candidate = build_merchant_plan(
            version,
            current["history"],
            current["rolling"],
            current["daily"],
            current["map"],
            source_url,
            current["notes"],
        )
        candidate.update({f"{key}_before": current[key] for key in ("history", "rolling", "daily")})
        plan = self.store.plan_once(version.version_id, "history", candidate)
        for key in ("history", "rolling", "daily"):
            compare = merchant_daily if key == "daily" else lambda rows: rows
            if not any(
                equal_rows(compare(current[key]), compare(plan[f"{key}_{state}"]))
                for state in ("before", "after")
            ):
                raise ValueError(f"{key} changed outside the saved publication plan")
            if self.limits and len(plan[f"{key}_after"]) > self.limits[key]:
                raise ValueError(f"{key} sheet row capacity must be extended before publication")
        self.store.assert_current(version.version_id)
        with suppress(GoogleAPIError):
            self.google.batch(DYNAMIC_ID, plan["dynamic_requests"])
        self.store.assert_current(version.version_id)
        if not equal_rows(merchant_daily(current["daily"]), merchant_daily(plan["daily_after"])):
            with suppress(GoogleAPIError):
                self.google.batch(ERP_ID, plan["erp_requests"])
        after = self.collect()
        # Target fields are owned by the independent Target source adapter.
        validation_plan = plan | {"daily_after": merchant_daily(plan["daily_after"])}
        validate_plan_readback(
            validation_plan, after["history"], after["rolling"], merchant_daily(after["daily"])
        )
        for attempt in range(6):
            try:
                self.verify_canonical(version)
                self.verify_inventory(plan["rolling_after"])
                break
            except ValueError:
                if attempt == 5:
                    raise
                self.sleep(2)
        self.store.assert_current(version.version_id)
        # The inventory source adapter owns the stock receipt. Demand/cover
        # verification above must not replace it with a rolling-sales digest.
        for destination, content in (
            ("history", plan["history_after"]),
            ("rolling", plan["rolling_after"]),
            ("erp", merchant_daily(plan["daily_after"])),
            ("canonical", [(r.sku, r.channel, r.units) for r in version.rows]),
        ):
            self.store.record_receipt(
                version.version_id, destination, digest(canonical(content)), version.version_id
            )
        return plan
