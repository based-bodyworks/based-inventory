import importlib.util
import unittest
from datetime import UTC, datetime
from pathlib import Path

MODULE = Path(__file__).with_name("collect_inventory_snapshot.py")


def load_collector():
    spec = importlib.util.spec_from_file_location("collect_inventory_snapshot", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InventorySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.collector = load_collector()
        self.now = datetime(2026, 10, 29, 22, 0, tzinfo=UTC)
        self.catalog = [{"product": "Conditioner", "sku": "44114437669093", "required": True}]
        self.rows = [
            {
                "sku": "44114437669093",
                "warehouse_id": "merchdrop",
                "warehouse_identifier": "Primary",
                "on_hand": 100,
                "available": 60,
                "allocated": 35,
                "reserve_inventory": 5,
                "backorder": 2,
                "sell_ahead": 0,
                "updated_at": "2026-10-29T21:59:00",
                "product": {"name": "Conditioner", "kit": False},
            },
            {
                "sku": "44114437669093",
                "warehouse_id": "office",
                "warehouse_identifier": "Primary",
                "on_hand": 8,
                "available": 8,
                "allocated": 0,
                "reserve_inventory": 0,
                "backorder": 0,
                "sell_ahead": 0,
                "updated_at": "2026-10-01T10:00:00",
                "product": {"name": "Conditioner", "kit": False},
            },
        ]

    def build(self, rows=None, collected_at=None, max_age_seconds=600):
        return self.collector.build_snapshot(
            self.catalog,
            self.rows if rows is None else rows,
            merchdrop_warehouse_id="merchdrop",
            collected_at=collected_at or self.now,
            now=self.now,
            max_age_seconds=max_age_seconds,
        )

    def test_keeps_physical_and_available_distinct_and_excludes_office_from_cover_pool(self):
        result = self.build()
        row = result["products"][0]
        self.assertEqual(row["on_hand"], 100)
        self.assertEqual(row["available"], 60)
        self.assertEqual(row["allocated"], 35)
        self.assertEqual(row["backorder"], 2)
        self.assertEqual(len(row["warehouse_stock"]), 2)
        self.assertEqual(
            result["pool"]["channels"], ["Shopify", "TikTok", "Amazon FBM", "Target replenishment"]
        )
        self.assertIn("stock already held at Target", result["pool"]["excluded"])
        self.assertNotIn("Target retail inventory", result["pool"]["excluded"])
        self.assertFalse(result["pool"]["target_retail_sellthrough_is_warehouse_depletion"])

    def test_target_mapping_must_cover_every_mapped_sku_once(self):
        target_map = {"CONDITIONER": {"product": "Conditioner", "sku": "44114437669093"}}
        coverage = self.collector.validate_target_inventory_coverage(self.catalog, target_map)
        self.assertEqual(coverage["status"], "Mapped catalog coverage only")
        self.assertFalse(coverage["target_warehouse_identity_verified"])
        self.assertEqual(coverage["skus"], ["44114437669093"])
        with self.assertRaisesRegex(ValueError, "Target.*MISSING"):
            self.collector.validate_target_inventory_coverage(
                self.catalog, {"NEW": {"product": "New", "sku": "MISSING"}}
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.collector.validate_target_inventory_coverage(
                self.catalog + self.catalog, target_map
            )

    def test_target_stock_is_required_and_never_fabricated_as_zero(self):
        target_map = {"CONDITIONER": {"product": "Conditioner", "sku": "44114437669093"}}
        with self.assertRaisesRegex(ValueError, "required"):
            self.collector.validate_target_inventory_coverage(
                [dict(self.catalog[0], required=False)], target_map
            )
        with self.assertRaisesRegex(ValueError, "missing.*44114437669093"):
            self.collector.build_snapshot(
                self.catalog,
                [],
                merchdrop_warehouse_id="merchdrop",
                collected_at=self.now,
                now=self.now,
                target_product_map=target_map,
            )

    def test_target_metadata_keeps_fresh_available_balance_without_sales_subtraction(self):
        target_map = {"CONDITIONER": {"product": "Conditioner", "sku": "44114437669093"}}
        snapshot = self.collector.build_snapshot(
            self.catalog,
            self.rows,
            merchdrop_warehouse_id="merchdrop",
            collected_at=self.now,
            now=self.now,
            target_product_map=target_map,
        )
        self.assertEqual(snapshot["products"][0]["available"], 60)
        self.assertEqual(
            snapshot["target_inventory_coverage"]["status"], "Mapped catalog coverage only"
        )
        self.assertFalse(
            snapshot["target_inventory_coverage"]["target_warehouse_identity_verified"]
        )
        self.assertIn("Do not subtract", snapshot["pool"]["available_rule"])

    def test_saved_catalog_covers_all_nine_current_target_skus(self):
        import json

        catalog = json.loads(MODULE.with_name("inventory_catalog.json").read_text())
        target_map = json.loads(MODULE.with_name("target_product_map.json").read_text())
        coverage = self.collector.validate_target_inventory_coverage(catalog, target_map)
        self.assertEqual(coverage["product_count"], 9)

    def binding(self, **overrides):
        row = {
            "report_label": "CONDITIONER",
            "report_sku": "44114437669093",
            "inventory_sku": "44114437669093",
            "identity_status": "verified_retail_alias",
            "provider_product_name": "Conditioner",
            "provider_kit": False,
            "evidence": "live provider SKU and title reviewed",
        }
        row.update(overrides)
        return {"bindings": [row]}

    def test_target_bindings_check_current_provider_identity_and_kit(self):
        target_map = {"CONDITIONER": {"product": "Conditioner", "sku": "44114437669093"}}
        check = self.collector.validate_target_inventory_bindings(
            self.catalog, target_map, self.binding(), self.rows, "merchdrop"
        )
        self.assertEqual(check["verified_count"], 1)
        with self.assertRaisesRegex(ValueError, "title"):
            self.collector.validate_target_inventory_bindings(
                self.catalog,
                target_map,
                self.binding(provider_product_name="Other"),
                self.rows,
                "merchdrop",
            )
        with self.assertRaisesRegex(ValueError, "kit"):
            self.collector.validate_target_inventory_bindings(
                self.catalog, target_map, self.binding(provider_kit=True), self.rows, "merchdrop"
            )

    def test_unresolved_target_binding_never_publishes_candidate_balance_as_target(self):
        target_map = {"CONDITIONER": {"product": "Conditioner", "sku": "44114437669093"}}
        binding = self.binding(
            identity_status="unresolved",
            inventory_sku=None,
            candidate_inventory_sku="44114437669093",
        )
        snapshot = self.collector.build_snapshot(
            self.catalog,
            self.rows,
            merchdrop_warehouse_id="merchdrop",
            collected_at=self.now,
            now=self.now,
            target_product_map=target_map,
            target_inventory_bindings=binding,
        )
        self.assertEqual(snapshot["target_inventory_coverage"]["unresolved_count"], 1)
        self.assertIsNone(snapshot["target_inventory"][0]["available"])
        self.assertIsNone(snapshot["target_inventory"][0]["inventory_sku"])

    def test_target_binding_set_must_match_current_report_map(self):
        target_map = {"CONDITIONER": {"product": "Conditioner", "sku": "44114437669093"}}
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.collector.validate_target_inventory_bindings(
                self.catalog, target_map, {"bindings": []}, self.rows, "merchdrop"
            )

    def test_target_order_identity_requires_fulfilled_order_with_matching_sku(self):
        target_map = {"CONDITIONER": {"product": "Conditioner", "sku": "44114437669093"}}
        binding = self.binding(
            identity_status="verified_target_order",
            verified_order={
                "id": "order1",
                "order_number": "TARGET-PO-1",
                "fulfillment_status": "fulfilled",
                "sku": "44114437669093",
            },
        )
        check = self.collector.validate_target_inventory_bindings(
            self.catalog, target_map, binding, self.rows, "merchdrop"
        )
        self.assertEqual(check["verified_count"], 1)
        binding["bindings"][0]["verified_order"]["fulfillment_status"] = "canceled"
        with self.assertRaisesRegex(ValueError, "fulfilled Target order"):
            self.collector.validate_target_inventory_bindings(
                self.catalog, target_map, binding, self.rows, "merchdrop"
            )

    def test_inventory_table_range_grows_to_exact_catalog_rows(self):
        requests = self.collector.sheet_requests(self.build())
        table = [r["updateTable"] for r in requests if "updateTable" in r]
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0]["table"]["range"]["startRowIndex"], 6)
        self.assertEqual(table[0]["table"]["range"]["endRowIndex"], 8)
        self.assertEqual(table[0]["table"]["range"]["endColumnIndex"], 23)

    def test_each_published_row_preserves_its_actual_capture_timestamp(self):
        result = self.collector.build_snapshot(
            self.catalog,
            self.rows,
            merchdrop_warehouse_id="merchdrop",
            collected_at=self.now,
            now=self.now,
            captured_at_by_sku={"44114437669093": "2026-10-29T21:59:30Z"},
        )
        self.assertEqual(self.collector.snapshot_source_rows(result)[0][7], "2026-10-29T21:59:30Z")
        with self.assertRaisesRegex(ValueError, "capture timestamp"):
            self.collector.build_snapshot(
                self.catalog,
                self.rows,
                merchdrop_warehouse_id="merchdrop",
                collected_at=self.now,
                now=self.now,
                captured_at_by_sku={},
            )

    def test_source_refresh_notice_uses_current_binding_counts(self):
        snapshot = self.build()
        snapshot["target_inventory_coverage"] = {"verified_count": 7, "unresolved_count": 2}
        requests = self.collector.sheet_requests(snapshot)
        notice = [
            r["updateCells"]
            for r in requests
            if r.get("updateCells", {}).get("start", {}).get("rowIndex") == 5
        ]
        self.assertEqual(len(notice), 1)
        self.assertEqual(notice[0]["start"]["columnIndex"], 1)
        self.assertIn(
            "7 verified, 2 unverified",
            notice[0]["rows"][0]["values"][0]["userEnteredValue"]["stringValue"],
        )

    def test_missing_required_warehouse_row_fails_instead_of_becoming_zero(self):
        with self.assertRaisesRegex(ValueError, "missing.*44114437669093"):
            self.build(rows=self.rows[1:])

    def test_missing_quantity_fails_instead_of_becoming_zero(self):
        rows = [dict(self.rows[0], available=None)]
        with self.assertRaisesRegex(ValueError, "available"):
            self.build(rows=rows)

    def test_duplicate_warehouse_record_fails_instead_of_double_counting(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.build(rows=[*self.rows, self.rows[0]])

    def test_old_collection_is_rejected_but_unchanged_sku_timestamp_is_preserved(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            self.build(collected_at=datetime(2026, 10, 29, 20, 0, tzinfo=UTC))
        result = self.build()
        self.assertEqual(
            result["products"][0]["warehouse_stock"][1]["updated_at"], "2026-10-01T10:00:00"
        )

    def test_unmapped_optional_product_is_unavailable_instead_of_zero(self):
        self.catalog.append({"product": "Unreleased Product", "sku": None, "required": False})
        row = self.build()["products"][1]
        self.assertIsNone(row["on_hand"])
        self.assertIsNone(row["available"])
        self.assertEqual(row["status"], "SKU not mapped")

    def test_output_rows_keep_numbers_and_snapshot_time_without_fabricating_fba_zero(self):
        result = self.build()
        self.assertEqual(result["products"][0]["amazon_fba"]["status"], "Not verified")
        rows = self.collector.snapshot_source_rows(result)
        self.assertEqual(
            rows[0][0:7], ["44114437669093", "Conditioner", "merchdrop", 100, 60, 35, 2]
        )
        self.assertEqual(rows[0][7], "2026-10-29T22:00:00Z")

    def test_fba_visibility_excludes_other_marketplaces_and_never_enters_merchdrop_pool(self):
        result = self.collector.build_snapshot(
            self.catalog,
            self.rows,
            merchdrop_warehouse_id="merchdrop",
            collected_at=self.now,
            now=self.now,
            fba_by_sku={
                "44114437669093": [
                    {"id": "us", "quantity": 7, "marketplace_id": "ATVPDKIKX0DER"},
                    {"id": "ca", "quantity": 11, "marketplace_id": "A2EUQ1WTGCTBG2"},
                ]
            },
        )
        self.assertEqual(result["products"][0]["amazon_fba"]["on_hand"], 7)
        self.assertEqual(result["products"][0]["available"], 60)

    def test_pacific_clock_offset_changes_with_daylight_saving_for_sheet_freshness(self):
        self.assertEqual(self.build()["pacific_to_utc_hours"], 7)
        after = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)
        result = self.collector.build_snapshot(
            self.catalog,
            self.rows,
            merchdrop_warehouse_id="merchdrop",
            collected_at=after,
            now=after,
        )
        self.assertEqual(result["pacific_to_utc_hours"], 8)


if __name__ == "__main__":
    unittest.main()
