import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_target_readback import verify


class TargetNativeReadbackTests(unittest.TestCase):
    def fixture(self):
        p = {
            "safe_to_write": True,
            "sheets_date_serial": 46329,
            "date": "2026-11-03",
            "message_id": "report",
            "derived_daily_units": 12,
            "products": [
                {
                    "sku": "SKU",
                    "product": "Product",
                    "daily_units": 12,
                    "daily_retail": 120,
                    "week_units": 39,
                    "week_retail": 390,
                    "week_forecast_units": 42.5,
                    "out_of_stock": 0.03,
                }
            ],
        }
        vals = [
            46329,
            "Product",
            "SKU",
            12,
            120,
            39,
            390,
            42.5,
            0.03,
            "report",
            "https://mail.google.com/#all/report",
            "derived",
        ]
        cells = [
            {"effectiveValue": {"numberValue" if isinstance(v, int | float) else "stringValue": v}}
            for v in vals
        ]
        n = {
            "spreadsheetId": "1TE-gFZLELr1BYfaTkhaAqhSkvuid7a8X0cZthyA33I4",
            "sheets": [
                {
                    "properties": {"sheetId": 560223170},
                    "data": [{"startRow": 10, "rowData": [{"values": cells}]}],
                }
            ],
        }
        return p, n

    def test_exact_daily_and_week_values_pass(self):
        p, n = self.fixture()
        self.assertTrue(verify(p, n)["daily_and_week_fields_verified"])

    def test_swapped_daily_and_week_columns_fail(self):
        p, n = self.fixture()
        cells = n["sheets"][0]["data"][0]["rowData"][0]["values"]
        cells[3:7] = cells[5:7] + cells[3:5]
        with self.assertRaisesRegex(ValueError, "column 4"):
            verify(p, n)

    def test_missing_duplicate_and_wrong_source_fail(self):
        p, n = self.fixture()
        n["sheets"][0]["data"][0]["rowData"] *= 2
        with self.assertRaisesRegex(ValueError, "duplicate"):
            verify(p, n)
        p, n = self.fixture()
        n["sheets"][0]["data"][0]["rowData"] = []
        with self.assertRaisesRegex(ValueError, "missing"):
            verify(p, n)
        p, n = self.fixture()
        n["sheets"][0]["data"][0]["rowData"][0]["values"][9]["effectiveValue"] = {
            "stringValue": "old-report"
        }
        with self.assertRaisesRegex(ValueError, "column 10"):
            verify(p, n)

    def test_text_date_cannot_masquerade_as_numeric_date(self):
        p, n = self.fixture()
        n["sheets"][0]["data"][0]["rowData"][0]["values"][0]["effectiveValue"] = {
            "stringValue": "2026-11-03"
        }
        with self.assertRaisesRegex(ValueError, "missing"):
            verify(p, n)


if __name__ == "__main__":
    unittest.main()
