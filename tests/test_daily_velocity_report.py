"""Reader-facing daily velocity uses exact physical SKUs and verified UTC days."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from html import escape

import pytest

from based_inventory.pipeline.models import Coverage, SalesRow, VerifiedDay


def version(day=date(2026, 11, 17), rows=()):
    return VerifiedDay(
        day=day,
        rows=tuple(rows),
        coverage=Coverage(10, 10, 10, True),
        excluded_orders={},
        catalog_hash="a" * 64,
        extractor_commit="b" * 40,
        report=b"verified workbook",
        captured_at=datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
    )


def catalog(tmp_path, entries):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(entries))
    return path


def render(*args, **kwargs):
    from based_inventory.daily_velocity_report import render_report

    return render_report(*args, **kwargs)


def test_every_canonical_sku_is_visible_including_zero_sales():
    from based_inventory.daily_velocity_report import DEFAULT_CATALOG_PATH

    entries = json.loads(DEFAULT_CATALOG_PATH.read_text())
    out = render(version(), {})
    assert f"{len(entries)} tracked SKUs" in out
    assert len(entries) == 47
    for entry in entries:
        assert f"{escape(entry['product'], quote=False)}: *0* | n/a" in out
    assert "Target retail Sea Salt Spray: *0* | n/a" in out
    assert "Mini Sea Salt Spray - Dusk: *0* | n/a" in out


def test_exact_seven_complete_dates_determine_daily_average(tmp_path):
    end = date(2026, 11, 17)
    path = catalog(tmp_path, [{"product": "Sea Salt Spray", "sku": "SALT"}])
    history = {}
    for offset in range(7):
        day = end - timedelta(days=offset)
        history[day] = version(day, [SalesRow("SALT", "Salt", "Shopify", offset + 1)])
    # Outside dates must not leak into either the reported day or the average.
    history[end - timedelta(days=7)] = version(
        end - timedelta(days=7), [SalesRow("SALT", "Salt", "Shopify", 999)]
    )
    history[end + timedelta(days=1)] = version(
        end + timedelta(days=1), [SalesRow("SALT", "Salt", "Shopify", 999)]
    )
    # The selected report, not a conflicting current-day history value, wins.
    history[end] = version(end, [SalesRow("SALT", "Salt", "Shopify", 999)])
    today = version(end, [SalesRow("SALT", "Salt", "Shopify", 1)])
    out = render(today, history, path)
    assert "Sea Salt Spray: *1* | 4.0/day" in out
    assert "2026-11-11 through 2026-11-17 (UTC)" in out
    assert "7/7 verified days" in out
    assert "Total day units: *1*" in out
    assert "7-day average: *4.0/day*" in out


def test_missing_one_date_never_uses_a_six_day_divisor(tmp_path):
    end = date(2026, 11, 17)
    path = catalog(tmp_path, [{"product": "Sea Salt Spray", "sku": "SALT"}])
    history = {
        end - timedelta(days=n): version(
            end - timedelta(days=n), [SalesRow("SALT", "Salt", "Shopify", 70)]
        )
        for n in range(6)
    }
    out = render(history[end], history, path)
    assert "Sea Salt Spray: *70* | n/a" in out
    assert "6/7 verified days" in out
    assert "Missing: 2026-11-11" in out
    assert "10.0/day" not in out


def test_sku_identity_keeps_same_name_variants_separate(tmp_path):
    path = catalog(
        tmp_path,
        [{"product": "Lotion", "sku": "DAWN"}, {"product": "Lotion", "sku": "DUSK"}],
    )
    current = version(
        rows=[
            SalesRow("DAWN", "Lotion", "Shopify", 4),
            SalesRow("DUSK", "Lotion", "Shopify", 9),
        ]
    )
    out = render(current, {}, path)
    assert "Lotion (`DAWN`): *4* | n/a" in out
    assert "Lotion (`DUSK`): *9* | n/a" in out
    assert "Total day units: *13*" in out


def test_unknown_positive_sales_are_visible_and_totals_reconcile(tmp_path):
    path = catalog(tmp_path, [{"product": "Catalog item", "sku": "KNOWN"}])
    current = version(
        rows=[
            SalesRow("KNOWN", "Catalog item", "Shopify", 3),
            SalesRow("NEW", "New product", "TIKTOK", 6),
            SalesRow("NEW", "New product", "Amazon FBM", 1),
        ]
    )
    out = render(current, {}, path)
    assert "Other sold SKUs (mapping unverified)" in out
    assert "Unverified product mappings remain in the totals" in out
    assert "New product (`NEW`): *7* | n/a" in out
    assert "Total day units: *10*" in out
    assert "Shopify 3" in out and "TikTok 6" in out and "Amazon merchant-fulfilled 1" in out
    assert "1 additional sold SKU" in out


def test_unknown_sku_that_sold_earlier_in_window_remains_visible(tmp_path):
    path = catalog(tmp_path, [{"product": "Catalog item", "sku": "KNOWN"}])
    end = date(2026, 11, 17)
    history = {end - timedelta(days=n): version(end - timedelta(days=n)) for n in range(7)}
    history[end - timedelta(days=2)] = version(
        end - timedelta(days=2), [SalesRow("HIST", "Earlier item", "Shopify", 14)]
    )
    out = render(history[end], history, path)
    assert "Earlier item (`HIST`): *0* | 2.0/day" in out
    assert "Total day units: *0*" in out


def test_scope_defines_units_and_separates_stock_pools():
    out = render(version(), {})
    assert "complete UTC day" in out
    assert "Units ordered, including gifts" in out
    assert "before cancellation or return adjustments" in out
    assert "Stockouts can limit observed orders" in out
    assert "component SKUs" in out
    assert "not shipments or a forecast" in out
    assert "Amazon-held fulfillment sales" in out
    assert "Target store sales" in out
    assert "Target replenishment shipments" in out
    assert "excluded" in out
    assert "\u2014" not in out


def test_unknown_names_cannot_create_slack_mentions(tmp_path):
    path = catalog(tmp_path, [{"product": "Known", "sku": "KNOWN"}])
    current = version(rows=[SalesRow("NEW", "<!channel> & <@USER>", "Shopify", 1)])
    out = render(current, {}, path)
    assert "<!channel>" not in out and "<@USER>" not in out
    assert "&lt;!channel&gt; &amp; &lt;@USER&gt;" in out


def test_provider_product_names_cannot_insert_fake_report_sections(tmp_path):
    path = catalog(tmp_path, [{"product": "Known", "sku": "KNOWN"}])
    current = version(rows=[SalesRow("NEW", "New\n*Fake section*\u2014item", "Shopify", 1)])
    out = render(current, {}, path)
    assert "New *Fake section*-item (`NEW`): *1*" in out
    assert "\n*Fake section*" not in out
    assert "\u2014" not in out


@pytest.mark.parametrize("bad", ["wrong-key", date(2026, 11, 16)])
def test_history_key_must_identify_its_verified_day(bad):
    with pytest.raises(ValueError, match="history"):
        render(version(), {bad: version()})


def test_large_catalog_fails_instead_of_silently_dropping_rows(tmp_path):
    path = catalog(
        tmp_path,
        [{"product": f"Product {i} " + "x" * 100, "sku": f"SKU{i}"} for i in range(500)],
    )
    with pytest.raises(ValueError, match="Slack"):
        render(version(), {}, path)


def test_duplicate_catalog_sku_is_rejected(tmp_path):
    path = catalog(
        tmp_path, [{"product": "First", "sku": "DUP"}, {"product": "Second", "sku": "DUP"}]
    )
    with pytest.raises(ValueError, match="catalog"):
        render(version(), {}, path)
