from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta

import openpyxl
import pytest

from based_inventory.pipeline.models import Coverage, SalesRow, VerifiedDay

CHANNELS = ("Shopify", "TikTok", "Amazon", "Target")


def sales(day=date(2026, 11, 17), rows=(), timezone="America/Los_Angeles", variance=0):
    from based_inventory.all_channel_sales import SalesDay, decode_source_proof, encode_proof
    from tests.test_all_channel_sales import model

    payload = model(rows, day=day, timezone=timezone).payload()
    target = payload["sources"]["Target"]
    if target["component_units"] < variance:
        variance = -variance
    target.update(
        reported_daily_units=target["component_units"] - variance,
        derived_daily_units=target["component_units"],
        units_variance=variance,
        message_id="target-message",
        previous_message_id="target-prior-message",
    )
    proof = decode_source_proof(target)
    proof["parsed"].update(
        {
            key: target[key]
            for key in (
                "reported_daily_units",
                "derived_daily_units",
                "units_variance",
                "message_id",
                "previous_message_id",
            )
        }
    )
    target["raw_proof_gzip"], target["raw_sha256"] = encode_proof(proof)
    return SalesDay.from_payload(payload)


def catalog(tmp_path, rows):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(rows))
    return path


def render(*args, **kwargs):
    from based_inventory.all_channel_report import render_sales_report

    return render_sales_report(*args, **kwargs)


def build(*args, **kwargs):
    from based_inventory.all_channel_report import build_sales_workbook

    return build_sales_workbook(*args, **kwargs)


def merchant(day=date(2026, 11, 17), rows=()):
    return VerifiedDay(
        day,
        tuple(rows),
        Coverage(3, 3, 3, True),
        {},
        "a" * 64,
        "b" * 40,
        b"merchant report",
        datetime(2026, 11, 18, 12, tzinfo=UTC),
    )


def test_all_catalog_rows_and_channel_counts_remain_visible():
    from based_inventory.daily_velocity_report import DEFAULT_CATALOG_PATH

    rows = [
        SalesRow("BB2000S-1-6-1", "Salt", "Shopify", 2),
        SalesRow("BB2000S-1-6-1", "Salt", "TikTok", 3),
        SalesRow("BB2000S-1-6-1", "Salt", "Amazon", 5),
        SalesRow("BB2000S-1-6-1-RETAIL", "Retail salt", "Target", 7),
    ]
    out = render(sales(rows=rows), {})
    entries = json.loads(DEFAULT_CATALOG_PATH.read_text())
    assert "47 tracked SKUs" in out
    assert "Total reported units: *17*" in out
    assert "Sea Salt Spray: *10* | n/a" in out
    assert "Shopify 2 · TikTok 3 · Amazon 5 · Target 0" in out
    assert "Target retail Sea Salt Spray: *7* | n/a" in out
    for entry in entries:
        assert entry["product"].replace("&", "&amp;") in out
    assert "Retail packaging (online orders only)" not in out
    assert "*Retail packaging*" in out


def test_warehouse_context_is_never_added_to_all_channel_sales(tmp_path):
    path = catalog(tmp_path, [{"sku": "S", "product": "Salt"}])
    current = sales(rows=[SalesRow("S", "Salt", "Amazon", 9)])
    warehouse = merchant(rows=[SalesRow("S", "Salt", "Amazon FBM", 100)])
    out = render(current, {}, warehouse, path)
    assert "Total reported units: *9*" in out
    assert "Salt: *9* | n/a" in out
    assert "Warehouse-only units" in out
    assert "*100*" in out and "UTC" in out
    assert "109" not in out


def test_seven_complete_dates_use_daily_average_not_short_window(tmp_path):
    path = catalog(tmp_path, [{"sku": "S", "product": "Salt"}])
    end = date(2026, 11, 17)
    history = {
        end - timedelta(days=i): sales(
            end - timedelta(days=i), [SalesRow("S", "Salt", "TikTok", i + 1)]
        )
        for i in range(7)
    }
    out = render(history[end], history, catalog_path=path)
    assert "Salt: *1* | 4.0/day" in out
    assert "7/7 complete reporting dates" in out
    del history[end - timedelta(days=6)]
    out = render(history[end], history, catalog_path=path)
    assert "Salt: *1* | n/a" in out
    assert "Missing: 2026-11-11" in out


def test_history_with_a_different_online_timezone_is_unavailable(tmp_path):
    path = catalog(tmp_path, [{"sku": "S", "product": "Salt"}])
    end = date(2026, 11, 17)
    history = {end - timedelta(days=i): sales(end - timedelta(days=i)) for i in range(7)}
    history[end - timedelta(days=1)] = sales(end - timedelta(days=1), timezone="UTC")
    out = render(history[end], history, catalog_path=path)
    assert "6/7 complete reporting dates" in out
    assert "Online timezone differs on: 2026-11-16" in out
    assert "7-day average: n/a" in out


def test_unknown_and_same_name_skus_are_separate(tmp_path):
    path = catalog(
        tmp_path,
        [{"sku": "A", "product": "Lotion"}, {"sku": "B", "product": "Lotion"}],
    )
    current = sales(
        rows=[
            SalesRow("A", "Lotion", "Shopify", 2),
            SalesRow("B", "Lotion", "Target", 4),
            SalesRow("NEW", "New item", "Amazon", 3),
        ]
    )
    out = render(current, {}, catalog_path=path)
    assert "Lotion (`A`): *2* | n/a" in out
    assert "Lotion (`B`): *4* | n/a" in out
    assert "New item (`NEW`): *3* | n/a" in out
    assert "mapping unverified" in out
    assert "Total reported units: n/a (product mappings unverified)" in out


def test_period_and_count_definitions_are_clear():
    out = render(sales(variance=1), {})
    assert "America/Los_Angeles" in out
    assert "Target follows its retail report date" in out
    assert "Cancelled online orders are excluded" in out
    assert "free items" in out
    assert "before returns" in out
    assert "TikTok includes TikTok fulfillment" in out
    assert "Amazon includes Amazon and merchant fulfillment" in out
    assert "Target product total differs from its headline by 1 unit" in out
    assert (
        "Packs use recipes available at collection time; past recipe changes are not reconstructed."
        in out
    )
    assert "\u2014" not in out


def test_provider_text_cannot_insert_mentions_or_sections(tmp_path):
    path = catalog(tmp_path, [{"sku": "K", "product": "Known"}])
    current = sales(rows=[SalesRow("NEW", "<!channel>\n*Fake*\u2014item", "TikTok", 1)])
    out = render(current, {}, catalog_path=path)
    assert "<!channel>" not in out
    assert "&lt;!channel&gt; *Fake*-item" in out
    assert "\n*Fake*" not in out


def test_saved_workbook_keeps_every_number_scope_and_zero(tmp_path):
    path = catalog(tmp_path, [{"sku": "S", "product": "Salt"}, {"sku": "ZERO", "product": "Zero"}])
    current = sales(
        rows=[SalesRow("S", "Salt", "TikTok", 4), SalesRow("S", "Salt", "Amazon", 5)],
        variance=1,
    )
    warehouse = merchant(rows=[SalesRow("S", "Salt", "TIKTOK", 700)])
    raw = build(current, {}, warehouse, path)
    workbook = openpyxl.load_workbook(io.BytesIO(raw), data_only=False)
    assert workbook.sheetnames == ["SKU Velocity", "Sources", "Methods"]
    sheet = workbook["SKU Velocity"]
    values = list(sheet.values)
    header = next(row for row in values if row and row[0] == "Category")
    assert header[:9] == (
        "Category",
        "Product",
        "SKU",
        "Day units",
        "7-day average/day",
        "Shopify",
        "TikTok",
        "Amazon",
        "Target",
    )
    salt = next(row for row in values if len(row) > 2 and row[2] == "S")
    zero = next(row for row in values if len(row) > 2 and row[2] == "ZERO")
    assert salt[3:10] == (9, "n/a", 0, 4, 5, 0, 700)
    assert zero[3:10] == (0, "n/a", 0, 0, 0, 0, 0)
    assert sheet.freeze_panes is not None and sheet.auto_filter.ref
    text = "\n".join(
        str(cell.value) for ws in workbook for row in ws for cell in row if cell.value is not None
    )
    assert "America/Los_Angeles" in text and "retail report date" in text
    assert "target-message" in text and "1 unit" in text
    assert "raw_sha256" in text
    assert "Warehouse-only" in text
    assert "Cancelled online orders are excluded" in text
    methods_text = " ".join(str(cell.value) for row in workbook["Methods"] for cell in row)
    assert (
        "Packs use recipes available at collection time; past recipe changes are not reconstructed."
        in methods_text
    )
    assert all(cell.data_type != "f" for ws in workbook for row in ws for cell in row)
    workbook.close()


def test_workbook_provider_names_are_text_not_excel_formulas(tmp_path):
    path = catalog(tmp_path, [{"sku": "K", "product": "Known"}])
    current = sales(rows=[SalesRow("UNKNOWN", '=HYPERLINK("https://evil.example")', "Shopify", 1)])
    workbook = openpyxl.load_workbook(io.BytesIO(build(current, {}, catalog_path=path)))
    row = next(
        row for row in workbook["SKU Velocity"] if len(row) > 2 and row[2].value == "UNKNOWN"
    )
    assert row[1].data_type == "s"
    workbook.close()


def test_saved_workbook_does_not_claim_a_warehouse_source_when_none_is_attached():
    workbook = openpyxl.load_workbook(io.BytesIO(build(sales(), {})))
    definition = next(
        row[1].value for row in workbook["Methods"] if row[0].value == "Warehouse-only reference"
    )
    assert "unavailable for this report" in definition
    assert "date and scope are shown" not in definition
    assert not any(row[0].value == "Warehouse-only" for row in workbook["Sources"])
    workbook.close()


def test_numeric_sku_identifiers_keep_their_full_text_display(tmp_path):
    identifier = "00441266062650"
    path = catalog(tmp_path, [{"sku": identifier, "product": "Texture Powder"}])
    workbook = openpyxl.load_workbook(io.BytesIO(build(sales(), {}, catalog_path=path)))
    sku_cell = workbook["SKU Velocity"]["C9"]
    assert sku_cell.value == identifier
    assert sku_cell.data_type == "s"
    assert sku_cell.number_format == "@"
    workbook.close()


def test_workbook_complete_window_shows_total_and_numeric_daily_average(tmp_path):
    path = catalog(tmp_path, [{"sku": "S", "product": "Salt"}])
    end = date(2026, 11, 17)
    history = {
        end - timedelta(days=i): sales(
            end - timedelta(days=i), [SalesRow("S", "Salt", "Amazon", i + 1)]
        )
        for i in range(7)
    }
    workbook = openpyxl.load_workbook(io.BytesIO(build(history[end], history, catalog_path=path)))
    sheet = workbook["SKU Velocity"]
    assert sheet["A4"].value == "Total reported units: 1 | 7-day average: 4.0/day"
    assert sheet["D9"].value == 1
    assert sheet["E9"].value == 4
    workbook.close()


def test_workbook_formats_actual_collector_scope_and_cutoff_metadata():
    from based_inventory.all_channel_sales import SalesDay

    payload = sales().payload()
    payload["sources"]["Shopify"].update(
        scope={"cancelled_orders": "excluded", "zero_value_orders": "included"},
        data_through="2026-11-17T23:59:59-08:00",
        provider_query="https://example.test/report",
    )
    current = SalesDay.from_payload(payload)
    workbook = openpyxl.load_workbook(io.BytesIO(build(current, {})))
    values = {
        (row[0], row[1]): row[2]
        for row in workbook["Sources"].values
        if len(row) >= 3 and row[0] == "Shopify"
    }
    assert values[("Shopify", "scope")] == "cancelled orders: excluded; zero value orders: included"
    assert values[("Shopify", "data_through")] == "2026-11-17T23:59:59-08:00"
    assert values[("Shopify", "provider_query")] == "https://example.test/report"
    workbook.close()


def test_unverified_current_source_keeps_rows_but_withholds_total_and_average(tmp_path):
    from based_inventory.all_channel_sales import SalesDay

    path = catalog(tmp_path, [{"sku": "S", "product": "Salt"}])
    current = sales(rows=[SalesRow("S", "Salt", "Amazon", 9)])
    payload = current.payload()
    payload["sources"]["Amazon"]["issues"] = ["Physical product mapping requires review"]
    current = SalesDay.from_payload(payload)
    out = render(current, {}, catalog_path=path)
    assert "Salt: *9* | n/a" in out
    assert "Total reported units: n/a (product mappings unverified)" in out
    assert "Day channels: Shopify 0 · TikTok 0 · Amazon n/a · Target 0" in out


def test_oversize_report_fails_without_dropping_products(tmp_path):
    path = catalog(
        tmp_path, [{"sku": str(i), "product": "Product " + "x" * 200 + str(i)} for i in range(500)]
    )
    with pytest.raises(ValueError, match="Slack"):
        render(sales(), {}, catalog_path=path)


def test_sources_do_not_present_shopify_only_exclusions_as_other_channel_controls():
    from based_inventory.all_channel_sales import SalesDay, decode_source_proof, encode_proof

    baseline = sales(rows=[SalesRow("BB2000S-1-6-1", "Sea Salt Spray", "TikTok", 3)])
    payload = baseline.payload()
    fields = ("excluded_mirrored_listing_units", "excluded_promotional_listing_units")
    for channel in ("Shopify", "TikTok", "Amazon"):
        source = payload["sources"][channel]
        proof = decode_source_proof(source)
        for field in fields:
            source[field] = proof[field] = 0
        source["raw_proof_gzip"], source["raw_sha256"] = encode_proof(proof)
        source["scope"] = {
            "promotional_orders": "known creator gifting excluded"
            if channel == "Shopify"
            else "not separately identified"
        }
    current = SalesDay.from_payload(payload)
    before = openpyxl.load_workbook(io.BytesIO(build(baseline, {})))
    after = openpyxl.load_workbook(io.BytesIO(build(current, {})))
    rows = list(after["Sources"].values)
    for field in fields:
        assert any(row[0] == "Shopify" and row[1] == field and row[2] == 0 for row in rows)
        assert not any(row[0] in ("TikTok", "Amazon") and row[1] == field for row in rows)
    for channel in ("TikTok", "Amazon"):
        assert any(
            row[0] == channel and row[1] == "scope" and "not separately identified" in row[2]
            for row in rows
        )
    assert list(before["SKU Velocity"].values) == list(after["SKU Velocity"].values)
    before.close()
    after.close()
