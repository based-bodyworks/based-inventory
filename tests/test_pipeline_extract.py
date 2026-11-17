import csv
import json
from datetime import UTC, date, datetime

import openpyxl
import pytest

from based_inventory.pipeline.extract import load_extract


def package(tmp_path):
    stem = tmp_path / "daily-sales-velocity_2026-11-16_2026-11-16"
    checkpoint = {
        "schema": 7,
        "warehouse_id": "V2FyZWhvdXNlOjExNzY2MQ==",
        "definition": "ordered",
        "catalog_fingerprint": "a" * 64,
        "days": {"2026-11-16": {"basedbodyworks.myshopify.com": {"sku": 4}}},
        "source_order_counts": {"2026-11-16": 2},
        "excluded_shops": {"Manual Order": 1},
        "source_coverage": {
            "2026-11-16": {"before": 2, "after": 2, "fetched_unique": 2, "complete": True}
        },
    }
    stem.with_suffix(".checkpoint.json").write_text(json.dumps(checkpoint))
    with stem.with_suffix(".csv").open("w") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", "SKU", "Channel", "QTY SOLD", "SKU CODE"])
        writer.writerow(["2026-11-16", "Product", "SHOPIFY", 4, "sku"])
    w = openpyxl.Workbook()
    w.active.title = "ABOUT"
    s = w.create_sheet("DAILY VELOCITY - SHOPIFY")
    s.append(["Date", "SKU", "QTY SOLD", "SKU CODE"])
    s.append(["2026-11-16", "Product", 4, "sku"])
    w.save(stem.with_suffix(".xlsx"))
    return stem, checkpoint


def load(stem):
    return load_extract(stem, date(2026, 11, 16), "b" * 40, datetime(2026, 11, 17, tzinfo=UTC))


def test_extract_checks_workbook_csv_checkpoint_and_full_coverage(tmp_path):
    stem, checkpoint = package(tmp_path)
    assert load(stem).totals["Shopify"] == 4
    checkpoint["source_coverage"] = {}
    stem.with_suffix(".checkpoint.json").write_text(json.dumps(checkpoint))
    with pytest.raises(ValueError, match="coverage"):
        load(stem)


def test_mismatched_csv_or_workbook_cannot_be_published(tmp_path):
    stem, _ = package(tmp_path)
    w = openpyxl.load_workbook(stem.with_suffix(".xlsx"))
    w["DAILY VELOCITY - SHOPIFY"]["C2"] = 999
    w.save(stem.with_suffix(".xlsx"))
    with pytest.raises(ValueError, match="workbook"):
        load(stem)
