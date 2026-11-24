from datetime import UTC, datetime

from based_inventory.pipeline.cloud_helpers.collect_inventory_snapshot import build_snapshot
from based_inventory.pipeline.target_refresh import asset


def test_duffel_stock_is_collected_separately_from_toiletry_bags():
    quantities = {
        "BB-ACCS-DUFBG-BLK": 17,
        "BB-ACCS-TTBAG-BLK": 101,
        "BB-ACCS-TTBAG-WHT": 203,
    }
    catalog = [item for item in asset("inventory_catalog.json") if item["sku"] in quantities]
    now = datetime(2026, 11, 24, 18, tzinfo=UTC)
    rows = [
        {
            "sku": sku,
            "warehouse_id": "warehouse",
            "on_hand": quantity,
            "available": quantity - 1,
            "allocated": 1,
            "reserve_inventory": 0,
            "backorder": 0,
            "sell_ahead": 0,
            "updated_at": now.isoformat(),
            "product": {"name": sku, "kit": False},
        }
        for sku, quantity in quantities.items()
    ]
    snapshot = build_snapshot(
        catalog, rows, collected_at=now, now=now, merchdrop_warehouse_id="warehouse"
    )
    products = {item["sku"]: item for item in snapshot["products"]}
    assert set(products) == set(quantities)
    assert products["BB-ACCS-DUFBG-BLK"]["product"] == "Duffel Bag"
    assert {sku: item["on_hand"] for sku, item in products.items()} == quantities
    assert products["BB-ACCS-DUFBG-BLK"]["available"] == 16
