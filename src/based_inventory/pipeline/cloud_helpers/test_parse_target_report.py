import copy
import unittest
from pathlib import Path

from parse_target_report import ReportError, derive_daily, parse_report

FIX = Path(__file__).with_name("fixtures")
MAP = {
    "SANTAL SEA SALT SPRAY": {"product": "Sea Salt Spray", "sku": "BB2000S-1-6-1"},
    "LEAVE-IN CONDITIONER": {"product": "Leave-In Conditioner", "sku": "BB-LEAVEIN-ONE"},
    "TEXTURE POWDER": {"product": "Texture Powder", "sku": "44126606262501"},
    "CURL CREAM": {"product": "Curl Cream", "sku": "BB-CC-SINGLE"},
    "MINI-SEA SALT SPRAY-DUSK": {
        "product": "Mini Sea Salt Spray - Dusk",
        "sku": "BB-SSS-MINI-DUSK",
    },
    "MINI-BODY LOTION-DUSK": {"product": "Mini Body Lotion - Dusk", "sku": "BB-BDYLTN-MINI-DUSK"},
    "MINI-LEAVE-IN COND": {"product": "Mini Leave-In Conditioner", "sku": "BB-LI-MINI"},
    "MINI-SKIN REVIVAL SPRAY": {"product": "Mini Skin Revival Spray", "sku": "BB-SRS-MINI"},
    "MINI-CURL CREAM": {"product": "Mini Curl Cream - Santal", "sku": "BB-CC-MINI-SANTAL"},
}


class TargetParserTests(unittest.TestCase):
    def reports(self):
        return (
            parse_report((FIX / "target_oct28_2026.txt").read_text(), "2026-10-28", "current"),
            parse_report((FIX / "target_oct27_2026.txt").read_text(), "2026-10-27", "previous"),
        )

    def test_added_minis_are_not_dropped_and_source_difference_is_retained(self):
        cur, prev = self.reports()
        result = derive_daily(cur, prev, MAP)
        self.assertEqual(len(result["products"]), 9)
        self.assertEqual(result["reported_daily_units"], 5451)
        self.assertEqual(result["derived_daily_units"], 5453)
        self.assertEqual(result["units_variance"], 2)
        self.assertEqual(result["derived_daily_retail"], 97209)
        self.assertEqual([x["daily_units"] for x in result["products"][-5:]], [3, 2, 1, 0, 0])
        self.assertTrue(all(x["prior_zero_proven"] for x in result["products"][-5:]))

    def test_sheet_date_is_numeric_and_matches_report_day(self):
        cur, prev = self.reports()
        result = derive_daily(cur, prev, MAP)
        self.assertEqual(result["sheets_date_serial"], 46323)
        self.assertIsInstance(result["sheets_date_serial"], int)

    def test_no_daily_retail_or_summary_asp_is_invented(self):
        cur, _ = self.reports()
        self.assertIsNone(cur["reported_daily_retail"])
        self.assertIsNone(cur["summary_asp"])
        self.assertEqual(cur["grand"]["asp"], 17.83)
        self.assertEqual(cur["products"][1]["week_forecast_units"], 7913.5)

    def test_newest_table_wins_over_quoted_history(self):
        first = (FIX / "target_oct28_2026.txt").read_text()
        old = (FIX / "target_oct27_2026.txt").read_text()
        cur = parse_report(first + "\nFrom: earlier sender\n" + old, "2026-10-28")
        self.assertEqual(len(cur["products"]), 9)
        self.assertEqual(cur["grand"]["week_units"], 25369)

    def test_incomplete_prior_table_cannot_prove_new_product_zero(self):
        cur, prev = self.reports()
        prev["category_complete"] = False
        with self.assertRaises(ReportError):
            derive_daily(cur, prev, MAP)

    def test_sunday_uses_current_week_to_date_instead_of_prior_week(self):
        cur, _ = self.reports()
        cur = copy.deepcopy(cur)
        cur["date"] = "2026-11-01"
        result = derive_daily(cur, None, MAP)
        self.assertEqual(result["derived_daily_units"], 25369)
        self.assertTrue(
            all(x["method"] == "Sunday week-to-date equals daily" for x in result["products"])
        )

    def test_unknown_product_is_retained_and_blocks_automatic_write(self):
        cur, prev = self.reports()
        result = derive_daily(cur, prev, {k: v for k, v in MAP.items() if k != "MINI-CURL CREAM"})
        self.assertEqual(len(result["products"]), 9)
        self.assertEqual(result["unmapped_products"], ["MINI-CURL CREAM"])
        self.assertFalse(result["safe_to_write"])

    def test_missing_table_or_bad_grand_total_is_rejected(self):
        with self.assertRaises(ReportError):
            parse_report("comment only", "2026-10-28")
        bad = (FIX / "target_oct28_2026.txt").read_text().replace("$452,312", "$452,313")
        with self.assertRaises(ReportError):
            parse_report(bad, "2026-10-28")

    def test_nonadjacent_prior_date_is_rejected(self):
        cur, prev = self.reports()
        prev["date"] = "2026-10-26"
        with self.assertRaises(ReportError):
            derive_daily(cur, prev, MAP)

    def test_extra_columns_after_oos_are_parsed_by_header_name(self):
        text = """Daily Sales as of 10.29:\n5 units sold yesterday\nWeek to Date Performance:\nRow Labels\n$ ty\nU ty\nU frst\n% to fcst\nasp\nlocs trk\noos locs\noos%\n$ promo\nU promo\n% wip (avg)\nSANTAL SEA SALT SPRAY\n$90\n5\n6\n-16.7%\n$18.00\n10\n2\n20.0%\n$9\n1\n80.0%\nGrand Total\n$90\n5\n6\n-16.7%\n$18.00\n10\n2\n20.0%\n$9\n1\n80.0%\n"""
        parsed = parse_report(text, "2026-10-29")
        self.assertEqual(parsed["products"][0]["out_of_stock"], 0.2)
        self.assertEqual(parsed["products"][0]["asp"], 18)

    def test_blank_trailing_oos_cells_are_allowed(self):
        text = """Daily Sales as of 11.1:
5 units sold yesterday
Week to Date Performance:
Row Labels
$ ty
U ty
U fcst
% to fcst
asp
locs trk
oos locs
oos%
SANTAL SEA SALT SPRAY
$90
5
6
-16.7%
$18.00
0
0
Grand Total
$90
5
6
-16.7%
$18.00
0
0
"""
        parsed = parse_report(text, "2026-11-01")
        self.assertIsNone(parsed["products"][0]["out_of_stock"])
        self.assertIsNone(parsed["grand"]["out_of_stock"])

    def test_wrong_body_date_is_rejected_without_matching_subject(self):
        text = (FIX / "target_oct28_2026.txt").read_text()
        with self.assertRaises(ReportError):
            parse_report(text, "2026-10-29")

    def test_verified_subject_can_resolve_stale_body_date(self):
        text = (
            (FIX / "target_oct28_2026.txt")
            .read_text()
            .replace("Daily Sales as of 10.28", "Daily Sales as of 10.27")
        )
        source = {
            "payload": {
                "mime_type": "text/plain",
                "headers": [
                    {
                        "name": "Subject",
                        "value": "Based - Target Daily Sales Reporting - Updated through 10/28/2026",
                    }
                ],
                "body": {"content": text},
            }
        }
        parsed = parse_report(source, "2026-10-28")
        self.assertIn("body date", parsed["date_note"])
        prior = parse_report((FIX / "target_oct27_2026.txt").read_text(), "2026-10-27")
        self.assertEqual(derive_daily(parsed, prior, MAP)["date_note"], parsed["date_note"])


if __name__ == "__main__":
    unittest.main()
