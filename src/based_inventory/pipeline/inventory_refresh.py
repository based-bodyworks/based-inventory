"""Server stock collector using the vetted physical-SKU and retail bindings."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from .cloud_helpers.collect_inventory_snapshot import (
    build_snapshot,
    sheet_requests,
    snapshot_source_rows,
)
from .google_api import ERP_ID, GoogleAPIError
from .target_refresh import SourceIncomplete, asset, validate_mode, verify_requests


def collect_inventory(shiphero, *, now=None):
    catalog = asset("inventory_catalog.json")
    rows = []
    captures = {}
    # The vetted source uses exact-SKU queries with complete warehouse pagination.
    for item in catalog:
        sku = item.get("sku")
        if not sku or sku in captures:
            continue
        query = "query($sku:String!){warehouse_products(sku:$sku){data{edges{node{sku warehouse_id warehouse_identifier on_hand available allocated reserve_inventory backorder sell_ahead updated_at product{name kit}}}pageInfo{hasNextPage}}}}"
        payload = shiphero._execute(query, {"sku": sku})
        result = (payload.get("data", {}).get("warehouse_products") or {}).get("data")
        if result is None or result.get("pageInfo", {}).get("hasNextPage") is not False:
            raise SourceIncomplete("ShipHero stock response missing or truncated for " + sku)
        rows.extend(edge["node"] for edge in result.get("edges", []))
        captures[sku] = now or datetime.now(UTC)
    captured = now or datetime.now(UTC)
    return build_snapshot(
        catalog,
        rows,
        collected_at=captured,
        now=captured,
        captured_at_by_sku=captures,
        target_product_map=asset("target_product_map.json"),
        target_inventory_bindings=asset("target_inventory_bindings.json"),
    )


def publish_inventory(snapshot, google, *, mode="compare", before_write=None):
    validate_mode(mode)
    tabs = [
        s["properties"]
        for s in google.metadata(ERP_ID).get("sheets", [])
        if s["properties"]["sheetId"] == 1740410290
    ]
    if len(tabs) != 1:
        raise SourceIncomplete("Current Inventory tab missing")
    a1 = "'" + tabs[0]["title"].replace("'", "''") + "'"
    before = google.cells(ERP_ID, [a1 + "!I8:P" + str(7 + len(snapshot["products"]))])
    requests = sheet_requests(snapshot)
    # Owner notes are preserved. Replace only this adapter's exact machine prefix.
    notes = google.cells(ERP_ID, [a1 + "!D8:H" + str(7 + len(snapshot["products"]))])
    for sheet in notes.get("sheets", []):
        for grid in sheet.get("data", []):
            for r, row in enumerate(grid.get("rowData", [])):
                row_index = grid.get("startRow", 0) + r
                for c, value in enumerate(row.get("values", [])):
                    note = value.get("note", "")
                    if note.startswith("[based.inventory.snapshot] "):
                        note = (
                            "[based.inventory.snapshot] Captured "
                            + snapshot["as_of_utc"]
                            + "; see current row source quantities."
                        )
                        requests.append(
                            {
                                "updateCells": {
                                    "start": {
                                        "sheetId": 1740410290,
                                        "rowIndex": row_index,
                                        "columnIndex": grid.get("startColumn", 0) + c,
                                    },
                                    "rows": [{"values": [{"note": note}]}],
                                    "fields": "note",
                                }
                            }
                        )
    frozen_formulas = [
        [c.get("userEnteredValue", {}).get("formulaValue") for c in r.get("values", [])]
        for sh in before.get("sheets", [])
        for g in sh.get("data", [])
        for r in g.get("rowData", [])
    ]
    if not frozen_formulas or any(not any(r) for r in frozen_formulas):
        raise SourceIncomplete("Current Inventory I:P formulas are missing before refresh")
    if mode == "publish" and before_write:
        before_write(
            {
                "requests": {ERP_ID: requests},
                "inventory_formulas": frozen_formulas,
                "inventory_range": a1 + "!I8:P" + str(7 + len(snapshot["products"])),
                "source_captured_at": snapshot["as_of_utc"],
            }
        )
    if mode == "publish":
        with contextlib.suppress(GoogleAPIError):
            google.batch(ERP_ID, requests)
    verify_requests(google, ERP_ID, requests)
    expected = snapshot_source_rows(snapshot)
    actual = google.values(ERP_ID, a1 + "!A8:H" + str(7 + len(expected)))

    def normalize(rows):
        return [[None if v == "" else v for v in row] for row in rows]

    if normalize(actual) != normalize(expected):
        raise SourceIncomplete(
            "Current Inventory native stock readback differs from captured physical-SKU source"
        )
    after = google.cells(ERP_ID, [a1 + "!I8:P" + str(7 + len(expected))])

    def formulas(native):
        return [
            [c.get("userEnteredValue", {}).get("formulaValue") for c in r.get("values", [])]
            for s in native.get("sheets", [])
            for g in s.get("data", [])
            for r in g.get("rowData", [])
        ]

    initial = formulas(before)
    if initial != formulas(after) or not initial or any(not any(row) for row in initial):
        raise SourceIncomplete("Current Inventory I:P formulas missing or changed")
    return {
        "status": "verified",
        "captured_at": snapshot["as_of_utc"],
        "rows": len(expected),
        "pool": snapshot["pool"],
        "verification": {
            "requests": {ERP_ID: requests},
            "inventory_formulas": initial,
            "inventory_range": a1 + "!I8:P" + str(7 + len(expected)),
        },
    }
