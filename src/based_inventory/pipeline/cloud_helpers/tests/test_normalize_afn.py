import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "normalize_afn.py"


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PATH.exists(), "AFN normalization helper is missing")
        spec = importlib.util.spec_from_file_location("normalize_afn", PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.catalog = [
            {"product": "Shampoo", "skus": ["SHAMPOO", "SHOWER - FBA"]},
            {"product": "Conditioner", "skus": ["CONDITIONER"]},
            {"product": "Texture Powder", "skus": ["POWDER"]},
        ]
        self.registry = {
            "live_kits": [
                {"sku": "SHOWER", "components": [["SHAMPOO", 1], ["CONDITIONER", 1]]},
                {"sku": "POWDER2", "components": [["POWDER", 2]]},
            ],
            "bundles": [],
        }

    def row(self, sku, units, day="2026-10-28"):
        return {"sku": sku, "date": day, "amazonsp_order_items.raw.quantity_ordered": units}

    def test_kit_components_take_priority_over_legacy_single_product_alias(self):
        result = self.module.normalize(
            [self.row("SHOWER - FBA - FNSKU", 3)], self.registry, self.catalog
        )
        self.assertEqual(
            {x["product"]: x["units"] for x in result["rows"]}, {"Shampoo": 3, "Conditioner": 3}
        )

    def test_pack_count_expands_to_physical_units(self):
        result = self.module.normalize([self.row("POWDER2-FBA", 4)], self.registry, self.catalog)
        self.assertEqual(result["rows"][0]["sku"], "POWDER")
        self.assertEqual(result["rows"][0]["units"], 8)
        self.assertEqual(result["ordered_listing_units"], 4)
        self.assertEqual(result["physical_units"], 8)

    def test_unknown_sku_cannot_silently_disappear(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN"):
            self.module.normalize([self.row("UNKNOWN", 2)], self.registry, self.catalog)

    def test_duplicate_date_sku_input_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.module.normalize(
                [self.row("POWDER", 2), self.row("POWDER", 3)], self.registry, self.catalog
            )

    def test_negative_quantity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "quantity"):
            self.module.normalize([self.row("POWDER", -1)], self.registry, self.catalog)

    def test_aliases_aggregate_without_losing_units(self):
        result = self.module.normalize(
            [self.row("POWDER", 2), self.row("POWDER-FBA", 3)], self.registry, self.catalog
        )
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["units"], 5)

    def source(self, rows, total):
        return {
            "rows": rows,
            "total": [{"amazonsp_order_items.raw.quantity_ordered": total}],
            "query_timezone": "UTC",
            "marketplace": "ATVPDKIKX0DER",
            "sales_channel": "Amazon.com",
            "fulfillment_channel": "AFN",
            "status_excluded": "Canceled",
            "metric": "amazonsp_order_items.raw.quantity_ordered",
            "date_from": "2026-10-28",
            "date_to": "2026-10-28",
        }

    def checked_source(self, source):
        fn = getattr(self.module, "normalize_source", None)
        self.assertTrue(callable(fn), "Source total, scope and date-window verification is missing")
        return fn(source, self.registry, self.catalog)

    def test_independent_provider_total_detects_a_dropped_row(self):
        with self.assertRaisesRegex(ValueError, "provider total"):
            self.checked_source(self.source([self.row("POWDER", 2)], 5))

    def test_missing_complete_day_is_not_treated_as_zero(self):
        source = self.source(
            [self.row("POWDER", 2, "2026-10-27"), self.row("POWDER", 2, "2026-10-29")], 4
        )
        source.update(date_from="2026-10-27", date_to="2026-10-29")
        with self.assertRaisesRegex(ValueError, "missing source date"):
            self.checked_source(source)

    def test_merchant_or_wrong_timezone_cannot_be_imported_as_afn(self):
        for field, value in [
            ("fulfillment_channel", "MFN"),
            ("query_timezone", "America/Los_Angeles"),
        ]:
            source = self.source([self.row("POWDER", 2)], 2)
            source[field] = value
            with self.assertRaisesRegex(ValueError, "scope"):
                self.checked_source(source)

    def test_complete_verified_source_preserves_provider_and_component_totals(self):
        result = self.checked_source(self.source([self.row("SHOWER - FBA", 3)], 3))
        self.assertEqual(result["provider_listing_units"], 3)
        self.assertEqual(result["physical_units"], 6)

    def test_truncated_query_cannot_publish(self):
        source = self.source([self.row("POWDER", 2)], 2)
        source["truncated"] = True
        with self.assertRaisesRegex(ValueError, "truncated"):
            self.checked_source(source)

    def test_snake_case_row_limit_flag_cannot_publish(self):
        source = self.source([self.row("POWDER", 2)], 2)
        source["row_limit_reached"] = True
        with self.assertRaisesRegex(ValueError, "truncated"):
            self.checked_source(source)


if __name__ == "__main__":
    unittest.main()
