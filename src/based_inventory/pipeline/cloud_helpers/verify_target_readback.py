"""Check native Target Daily cells against the independently parsed email table."""

import argparse
import json
import math
from pathlib import Path


def verify(parsed, native):
    if not parsed.get("safe_to_write"):
        raise ValueError("Target report has unresolved products")
    root = native.get("structuredContent", native)
    if root.get("spreadsheetId") != "1TE-gFZLELr1BYfaTkhaAqhSkvuid7a8X0cZthyA33I4":
        raise ValueError("Wrong Target source spreadsheet")
    sheets = [
        s for s in root.get("sheets", []) if s.get("properties", {}).get("sheetId") == 560223170
    ]
    if len(sheets) != 1:
        raise ValueError("Expected native Target Daily sheet")
    expected = {p["sku"]: p for p in parsed["products"]}
    if len(expected) != len(parsed["products"]) or not expected:
        raise ValueError("Parsed Target products must be nonempty and unique")
    found = {}

    def value(cell):
        v = cell.get("effectiveValue", {})
        if "errorValue" in v:
            raise ValueError("Target cell has a formula error")
        return v.get("numberValue", v.get("stringValue"))

    for grid in sheets[0].get("data", []):
        if grid.get("startColumn", 0) != 0:
            raise ValueError("Readback must include columns A through L")
        for row in grid.get("rowData", []):
            cells = row.get("values", [])
            if not cells or value(cells[0]) != parsed["sheets_date_serial"]:
                continue
            if len(cells) < 12:
                raise ValueError("Incomplete Target Daily row")
            values = [value(c) for c in cells]
            sku = values[2]
            if sku not in expected or sku in found:
                raise ValueError("Unexpected or duplicate Target product: " + str(sku))
            p = expected[sku]
            want = [
                parsed["sheets_date_serial"],
                p["product"],
                sku,
                p["daily_units"],
                p["daily_retail"],
                p["week_units"],
                p["week_retail"],
                p["week_forecast_units"],
                p["out_of_stock"],
                parsed["message_id"],
            ]
            for col, (actual, required) in enumerate(zip(values, want, strict=False), 1):
                if isinstance(required, int | float):
                    match = (
                        isinstance(actual, int | float)
                        and not isinstance(actual, bool)
                        and math.isfinite(actual)
                        and math.isclose(actual, required, rel_tol=0, abs_tol=1e-8)
                    )
                else:
                    match = actual == required
                if not match:
                    raise ValueError(
                        f"Target {sku} column {col}: {actual!r} != parsed source {required!r}"
                    )
            if parsed["message_id"] not in str(values[10]):
                raise ValueError("Target source link does not identify the current report")
            found[sku] = values
    if set(found) != set(expected):
        raise ValueError(
            "Readback is missing Target products: " + ", ".join(sorted(set(expected) - set(found)))
        )
    daily = sum(r[3] for r in found.values())
    week = sum(r[5] for r in found.values())
    if daily != parsed["derived_daily_units"]:
        raise ValueError("Target daily product total differs from parsed source")
    return {
        "pass": True,
        "date": parsed["date"],
        "products": len(found),
        "daily_units": daily,
        "week_units": week,
        "daily_and_week_fields_verified": True,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--parsed", required=True)
    ap.add_argument("--readback", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    result = verify(
        json.loads(Path(args.parsed).read_text()), json.loads(Path(args.readback).read_text())
    )
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
