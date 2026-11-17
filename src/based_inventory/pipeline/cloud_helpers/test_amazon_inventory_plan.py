import copy
import unittest
from datetime import UTC, datetime

from amazon_inventory_plan import build_plan, verify_readback

METRIC = "amazonsp_order_items.raw.quantity_ordered"
SOURCE = "223f61d4-92f0-408a-9c2b-c3b42a8e53c8"


class AmazonInventoryPlanTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(
            marketplace_id="ATVPDKIKX0DER",
            sku="sold-zero",
            asin="ASIN1",
            product_name="Verified single",
            condition="NewItem",
            fulfillable=0,
            reserved=4,
            inbound_working=10,
            inbound_shipped=2,
            inbound_receiving=3,
            unfulfillable=0,
            total=19,
        )
        self.snapshot = dict(
            connector_key="amazon-selling-partner",
            operation="inventory",
            datasource_id=SOURCE,
            account_label="account",
            cached=False,
            fetched_at="2026-10-29T23:16:02Z",
            row_count=1,
            truncated=False,
            rows=[self.row],
        )
        self.sales = dict(
            query_timezone="UTC",
            marketplace="ATVPDKIKX0DER",
            sales_channel="Amazon.com",
            fulfillment_channel="AFN",
            status_excluded="Canceled",
            metric=METRIC,
            date_from="2026-10-22",
            date_to="2026-10-28",
            truncated=False,
            row_limit_reached=False,
            rows=[dict(date=f"2026-10-{d}", sku="sold-zero", **{METRIC: 1}) for d in range(22, 29)],
        )
        self.annotations = {
            "ASIN1": {
                "label": "Single product",
                "kind": "single",
                "provider_product_title": "Verified single",
            }
        }
        self.now = datetime(2026, 10, 29, 23, 30, tzinfo=UTC)

    def plan(self):
        return build_plan(
            self.snapshot, self.sales, self.annotations, "2026-10-22", "2026-10-28", now=self.now
        )

    def test_same_asin_positive_alternate_prevents_product_stockout_claim(self):
        alternate = dict(self.row, sku="positive-alternate", fulfillable=6, total=25)
        self.snapshot["rows"].append(alternate)
        self.snapshot["row_count"] = 2
        result = self.plan()["zero_ready_recent_singles"][0]
        self.assertEqual(result["sku"], "sold-zero")
        self.assertEqual(result["positive_ready_alternates"], ["positive-alternate"])
        self.assertFalse(result["product_zero_ready"])

    def test_unsold_zero_listing_is_preserved_but_excluded_from_gap_list(self):
        self.snapshot["rows"].append(dict(self.row, sku="obsolete-zero"))
        self.snapshot["row_count"] = 2
        result = self.plan()
        self.assertEqual(len(result["expected_readback"]), 2)
        self.assertEqual([r["sku"] for r in result["zero_ready_recent_singles"]], ["sold-zero"])
        self.assertEqual(result["zero_ready_recent_singles"][0]["inbound"], 15)

    def test_truncated_inventory_is_rejected(self):
        self.snapshot["truncated"] = True
        with self.assertRaisesRegex(ValueError, "truncated"):
            self.plan()

    def test_readback_checks_every_original_field_without_kit_conversion(self):
        self.annotations["ASIN1"]["kind"] = "kit"
        result = self.plan()
        rows = copy.deepcopy(result["expected_readback"])
        self.assertEqual(rows[0][6:11], [4, 15, 10, 2, 3])
        self.assertEqual(result["zero_ready_recent_singles"], [])
        self.assertTrue(verify_readback(result, rows))
        rows[0][5] = 99
        with self.assertRaisesRegex(ValueError, "readback"):
            verify_readback(result, rows)

    def test_incomplete_sales_dates_and_unknown_recent_identity_are_rejected(self):
        self.sales["rows"].pop()
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.plan()
        self.sales["rows"].append(dict(date="2026-10-28", sku="sold-zero", **{METRIC: 1}))
        self.annotations.clear()
        with self.assertRaisesRegex(ValueError, "identity"):
            self.plan()

    def test_changed_provider_title_requires_identity_review(self):
        self.snapshot["rows"][0]["product_name"] = "A different product"
        with self.assertRaisesRegex(ValueError, "identity"):
            self.plan()

    def test_provider_total_must_match_inventory_states(self):
        self.snapshot["rows"][0]["total"] += 1
        with self.assertRaisesRegex(ValueError, "total"):
            self.plan()

    def test_inventory_older_than_thirty_minutes_is_rejected(self):
        self.snapshot["fetched_at"] = "2026-10-29T22:59:00Z"
        with self.assertRaisesRegex(ValueError, "stale"):
            self.plan()

    def test_wrong_connected_account_is_rejected(self):
        self.snapshot["datasource_id"] = "another-company"
        with self.assertRaisesRegex(ValueError, "account"):
            self.plan()

    def test_sales_window_must_be_latest_seven_completed_utc_days(self):
        for start, end in [("2026-10-01", "2026-10-07"), ("2026-11-01", "2026-11-07")]:
            with self.subTest(end=end):
                self.sales["date_from"], self.sales["date_to"] = start, end
                self.sales["rows"] = [
                    dict(date=start[:8] + f"{d:02}", sku="sold-zero", **{METRIC: 1})
                    for d in range(1, 8)
                ]
                with self.assertRaisesRegex(ValueError, "latest"):
                    build_plan(
                        self.snapshot, self.sales, self.annotations, start, end, now=self.now
                    )

    def test_revenue_metric_cannot_be_presented_as_ordered_units(self):
        self.sales["metric"] = "revenue"
        for row in self.sales["rows"]:
            row["revenue"] = 120
        with self.assertRaisesRegex(ValueError, "metric"):
            self.plan()

    def test_sales_quantities_must_be_finite_nonnegative_integers(self):
        for invalid in [float("nan"), float("inf"), 1.5, -1, True]:
            with self.subTest(quantity=invalid):
                self.sales["rows"][0][METRIC] = invalid
                with self.assertRaisesRegex(ValueError, "Invalid ordered"):
                    self.plan()


if __name__ == "__main__":
    unittest.main()
