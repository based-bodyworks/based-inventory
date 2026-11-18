from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock

import pytest

from based_inventory.pipeline.models import SalesRow
from based_inventory.pipeline.target_refresh import SourceIncomplete
from based_inventory.registry import BundleEntry, BundleRegistry

DAY = date(2026, 11, 17)
ZONE = "America/Los_Angeles"


def source(channel, rows=(), *, day=DAY, timezone=ZONE):
    from based_inventory.all_channel_sales import encode_proof

    raw_rows = [
        {"order_id": str(index), "sku": row.sku, "units": row.units}
        for index, row in enumerate(rows)
    ]
    raw = {"rows": raw_rows, "control_listing_units": sum(row.units for row in rows)}
    if channel == "Target":
        raw = {
            "parsed": {
                "date": str(day),
                "products": [{"daily_units": row.units} for row in rows],
                "derived_daily_units": sum(row.units for row in rows),
                "reported_daily_units": sum(row.units for row in rows),
                "units_variance": 0,
                "message_id": "target-report",
                "previous_message_id": "prior-target-report",
            }
        }
    proof, proof_hash = encode_proof(raw)
    result = {
        "status": "complete",
        "date": day.isoformat(),
        "timezone": "retail_report_date" if channel == "Target" else timezone,
        "normalized_rows": [asdict(row) for row in rows],
        "component_units": sum(row.units for row in rows),
        "source_row_count": len(raw_rows),
        "listing_units": sum(row.units for row in rows),
        "control_listing_units": sum(row.units for row in rows),
        "raw_proof_gzip": proof,
        "raw_sha256": proof_hash,
        "captured_at": datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        .replace(hour=12)
        .isoformat(),
        "issues": [],
    }
    if channel == "Target":
        result.update(
            {
                key: raw["parsed"][key]
                for key in (
                    "derived_daily_units",
                    "reported_daily_units",
                    "units_variance",
                    "message_id",
                    "previous_message_id",
                )
            }
        )
    return result


def model(rows=(), *, day=DAY, timezone=ZONE):
    from based_inventory.all_channel_sales import SalesDay

    return SalesDay(
        day,
        tuple(rows),
        {
            c: source(c, [r for r in rows if r.channel == c], day=day, timezone=timezone)
            for c in ("Shopify", "TikTok", "Amazon", "Target")
        },
        timezone,
    )


def empty_registry():
    return BundleRegistry((), frozenset(), {})


def test_sales_day_roundtrip_binds_every_channel_and_original_proof():
    from based_inventory.all_channel_sales import SalesDay

    first = model([SalesRow("SALT", "Sea Salt", "TikTok", 3)])
    assert first.complete
    assert first.totals == {"Shopify": 0, "TikTok": 3, "Amazon": 0, "Target": 0}
    assert SalesDay.from_payload(first.payload()) == first
    changed = first.payload()
    from based_inventory.all_channel_sales import decode_source_proof, encode_proof

    original_proof = decode_source_proof(changed["sources"]["TikTok"])
    proof, proof_hash = encode_proof(original_proof | {"different_source_capture": True})
    changed["sources"]["TikTok"]["raw_proof_gzip"] = proof
    changed["sources"]["TikTok"]["raw_sha256"] = proof_hash
    assert SalesDay.from_payload(changed).capture_id != first.capture_id
    with pytest.raises(TypeError):
        first.sources["TikTok"]["component_units"] = 99


def test_sales_day_rejects_missing_wrong_day_or_inconsistent_source():
    from based_inventory.all_channel_sales import SalesDay

    payload = model().payload()
    del payload["sources"]["Target"]
    with pytest.raises(ValueError, match="sources"):
        SalesDay.from_payload(payload)
    payload = model().payload()
    payload["sources"]["Target"]["date"] = "2026-11-16"
    with pytest.raises(ValueError, match="date"):
        SalesDay.from_payload(payload)
    payload = model().payload()
    payload["sources"]["Amazon"]["component_units"] = 3
    with pytest.raises(ValueError, match="units"):
        SalesDay.from_payload(payload)


def test_sales_day_preserves_unknown_positive_as_explicit_incomplete_mapping():
    from based_inventory.all_channel_sales import SalesDay

    payload = model([SalesRow("NEW", "Unmapped SKU NEW", "Shopify", 2)]).payload()
    payload["sources"]["Shopify"]["issues"] = ["Unmapped physical SKU: NEW"]
    result = SalesDay.from_payload(payload)
    assert not result.complete
    assert result.total_units == 2


@pytest.mark.parametrize("channel", ["Shopify", "TikTok", "Amazon"])
def test_native_kit_and_separately_sold_loose_units_are_both_counted(channel):
    from based_inventory.all_channel_sales import normalize_online_rows

    entry = BundleEntry("KIT", "Kit", "shiphero", (("SALT", "Salt", 2),), False)
    registry = BundleRegistry((entry,), frozenset({"KIT"}), {"KIT": entry})
    records = [
        {"order_id": "one", "sku": "KIT", "units": 1},
        {"order_id": "one", "sku": "SALT", "units": 2},
        {"order_id": "two", "sku": "KIT-FBA", "units": 1},
    ]
    rows, issues, excluded = normalize_online_rows(
        records, channel, registry, {"KIT"}, {"SALT": "Sea Salt"}, {}
    )
    assert rows == (SalesRow("SALT", "Sea Salt", channel, 6),)
    assert not issues and excluded == 0


def test_native_parent_and_child_kit_listings_are_separate_purchases():
    from based_inventory.all_channel_sales import normalize_online_rows

    child = BundleEntry("CHILD", "Child kit", "shiphero", (("SALT", "Salt", 2),), False)
    parent = BundleEntry("PARENT", "Parent kit", "shiphero", (("CHILD", "Child", 3),), False)
    registry = BundleRegistry(
        (child, parent), frozenset({"CHILD", "PARENT"}), {"CHILD": child, "PARENT": parent}
    )
    records = [
        {"order_id": "one", "sku": "PARENT", "units": 1},
        {"order_id": "one", "sku": "CHILD", "units": 2},
        {"order_id": "one", "sku": "SALT", "units": 1},
    ]
    rows, issues, _ = normalize_online_rows(
        records, "TikTok", registry, registry.bundle_skus, {"SALT": "Salt"}, {}
    )
    assert rows == (SalesRow("SALT", "Salt", "TikTok", 11),)
    assert not issues


@pytest.mark.parametrize("failure", ["missing", "partial", "cycle", "zero"])
def test_native_unresolved_or_invalid_recipe_stops_the_report(failure):
    from based_inventory.all_channel_sales import normalize_online_rows

    child = "KIT" if failure == "cycle" else "SALT"
    entry = BundleEntry(
        "KIT",
        "Kit",
        "shiphero",
        ((child, "Component", 0 if failure == "zero" else 2),),
        failure == "partial",
    )
    registry = (
        empty_registry()
        if failure == "missing"
        else BundleRegistry((entry,), frozenset({"KIT"}), {"KIT": entry})
    )
    with pytest.raises(SourceIncomplete, match="components"):
        normalize_online_rows(
            [{"order_id": "one", "sku": "KIT", "units": 1}],
            "Amazon",
            registry,
            {"KIT"},
            {"SALT": "Salt"},
            {},
        )


def test_native_tiktok_all_fulfillment_is_counted_once_and_never_adds_warehouse():
    from based_inventory.all_channel_sales import normalize_online_rows

    rows, _, _ = normalize_online_rows(
        [
            {"order_id": "seller", "sku": "SALT", "units": 2},
            {"order_id": "tiktok", "sku": "SALT", "units": 3},
        ],
        "TikTok",
        empty_registry(),
        set(),
        {"SALT": "Sea Salt"},
        {},
    )
    assert rows == (SalesRow("SALT", "Sea Salt", "TikTok", 5),)


def test_shopify_marketplace_mirrors_are_excluded_but_numeric_sources_are_not_guessed():
    from based_inventory.all_channel_sales import normalize_online_rows

    records = [
        {"order_id": "one", "sku": "SALT", "units": 2, "sales_channel_name": "TikTok Shop"},
        {"order_id": "two", "sku": "SALT", "units": 3, "sales_channel_name": "Amazon.com"},
        {"order_id": "three", "sku": "SALT", "units": 4, "sales_channel_name": "294412648449"},
    ]
    rows, issues, excluded = normalize_online_rows(
        records, "Shopify", empty_registry(), set(), {"SALT": "Sea Salt"}, {}
    )
    assert rows[0].units == 4 and excluded == 5 and not issues


def test_known_shopify_creator_and_pr_orders_are_separate_from_customer_sales():
    from based_inventory.all_channel_sales import (
        METRICS,
        collect_online_source,
        decode_source_proof,
    )

    metric = METRICS["Shopify"]
    records = [
        {"order_id": "creator", "sku": "SALT", metric: 3, "sales_channel_name": "73360408577"},
        {"order_id": "pr", "sku": "SALT", metric: 2, "sales_channel_name": "cortex-pr-orders"},
        {"order_id": "paid", "sku": "SALT", metric: 1, "sales_channel_name": "294412648449"},
        {"order_id": "customer-gift", "sku": "SALT", metric: 4, "sales_channel_name": "web"},
        {"order_id": "mirror", "sku": "SALT", metric: 6, "sales_channel_name": "Amazon"},
    ]
    result = collect_online_source(
        DAY,
        "Shopify",
        Polar(report(metric, records)),
        ZONE,
        empty_registry(),
        set(),
        {"SALT": "Salt"},
        {},
    )
    assert result["listing_units"] == result["control_listing_units"] == 16
    assert result["component_units"] == 5
    assert result["excluded_promotional_listing_units"] == 5
    assert result["excluded_mirrored_listing_units"] == 6
    proof = decode_source_proof(result)
    assert proof["excluded_promotional_listing_units"] == 5
    assert proof["excluded_mirrored_listing_units"] == 6
    assert "73360408577" in proof["promotional_source_labels"]


@pytest.mark.parametrize(
    "records",
    [
        [{"order_id": "one", "sku": "SALT", "units": -1}],
        [{"order_id": "one", "sku": "SALT", "units": 1.5}],
        [{"order_id": "", "sku": "SALT", "units": 1}],
        [{"order_id": "one", "sku": "SALT", "units": 1}] * 2,
    ],
)
def test_invalid_or_duplicate_native_rows_are_rejected(records):
    from based_inventory.all_channel_sales import normalize_online_rows

    with pytest.raises(SourceIncomplete):
        normalize_online_rows(records, "TikTok", empty_registry(), set(), {"SALT": "Sea Salt"}, {})


def report(metric, rows, total=None):
    return {
        "dateRange": {"start": str(DAY), "end": str(DAY)},
        "tableData": rows,
        "totalData": [{metric: total if total is not None else sum(row[metric] for row in rows)}],
        "dataThrough": {metric: "2026-11-18T12:00:00+00:00"},
        "deepLink": "https://app.polaranalytics.com/based/r/example",
    }


class Polar:
    def __init__(self, main, control=None):
        self.conversation = "conversation"
        self.main = main
        self.control = control if control is not None else main
        self.requests = []

    def call(self, tool, payload):
        self.requests.append((tool, payload))
        if tool == "list_dimensions":
            return {
                "dimensions": ["order_id", "sku", "product_title", "sales_channel_name"],
                "filters": ["is_cancelled", "is_gift_card"],
                "settings": {"zero_value_orders": {}},
            }
        return deepcopy(self.main if payload["dimensions"] else self.control)


def test_online_source_checks_control_total_filters_date_and_source_clock():
    from based_inventory.all_channel_sales import METRICS, collect_online_source

    metric = METRICS["TikTok"]
    raw = report(metric, [{"order_id": "one", "sku": "SALT", metric: 5}])
    polar = Polar(raw)
    result = collect_online_source(
        DAY, "TikTok", polar, ZONE, empty_registry(), set(), {"SALT": "Sea Salt"}, {}
    )
    assert result["component_units"] == 5
    assert result["timezone"] == ZONE
    assert result["scope"]["cancelled_orders"] == "excluded"
    assert "fulfillment_type" not in polar.requests[1][1]["rules"]
    assert '"is_cancelled"' in polar.requests[1][1]["rules"]
    assert polar.requests[2][1]["dimensions"] == []
    assert polar.requests[2][1]["limit"] == 10
    from based_inventory.all_channel_sales import decode_source_proof

    assert decode_source_proof(result)["control_query"] == polar.requests[2][1]
    broken = Polar(raw, report(metric, [], total=6))
    with pytest.raises(SourceIncomplete, match="control"):
        collect_online_source(
            DAY, "TikTok", broken, ZONE, empty_registry(), set(), {"SALT": "Sea Salt"}, {}
        )


@pytest.mark.parametrize(
    "change",
    [
        {"filtersNotApplied": [{"dimensionKeys": ["is_cancelled"]}]},
        {"dateRange": {"start": "2026-11-16", "end": "2026-11-16"}},
        {"dataThrough": {}},
        {"rowLimitReached": {"limit": 100000}},
        {"truncated": True},
    ],
)
def test_source_failures_cannot_become_complete_sales(change):
    from based_inventory.all_channel_sales import METRICS, collect_online_source

    metric = METRICS["TikTok"]
    raw = report(metric, [{"order_id": "one", "sku": "SALT", metric: 5}]) | change
    with pytest.raises(SourceIncomplete):
        collect_online_source(
            DAY, "TikTok", Polar(raw), ZONE, empty_registry(), set(), {"SALT": "Sea Salt"}, {}
        )


def test_target_rows_use_daily_units_and_verified_retail_bindings():
    from based_inventory.all_channel_sales import normalize_target_source

    parsed = {
        "date": str(DAY),
        "message_id": "message",
        "previous_message_id": "prior",
        "safe_to_write": True,
        "reported_daily_units": 5,
        "derived_daily_units": 6,
        "units_variance": 1,
        "products": [
            {"label": "SANTAL SEA SALT SPRAY", "sku": "SALT", "daily_units": 6, "week_units": 100}
        ],
    }
    result = normalize_target_source(
        DAY,
        parsed,
        {"SALT-RETAIL": "Sea Salt retail"},
        [
            {
                "report_label": "SANTAL SEA SALT SPRAY",
                "report_sku": "SALT",
                "inventory_sku": "SALT-RETAIL",
            }
        ],
    )
    assert result["normalized_rows"] == [
        asdict(SalesRow("SALT-RETAIL", "Sea Salt retail", "Target", 6))
    ]
    assert result["units_variance"] == 1
    parsed["date"] = "2026-11-16"
    with pytest.raises(SourceIncomplete, match="date"):
        normalize_target_source(DAY, parsed, {"SALT-RETAIL": "Sea Salt retail"}, [])


def test_download_never_sends_credentials_to_a_foreign_origin():
    from based_inventory.all_channel_sales import download_report_rows

    session = Mock()
    for url in [
        "http://api.polaranalytics.com/file",
        "https://evil.example/file",
        "https://user:password@api.polaranalytics.com/file",
    ]:
        with pytest.raises(SourceIncomplete, match="download"):
            download_report_rows({"url": url, "format": "csv", "maxRows": 100000}, session=session)
    session.get.assert_not_called()


def test_download_is_complete_bounded_and_refuses_redirects():
    from based_inventory.all_channel_sales import download_report_rows

    response = Mock(status_code=200)
    response.iter_content.return_value = [b"order_id,sku,units\none,SALT,2\n"]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    session = Mock()
    session.get.return_value = response
    rows, checksum = download_report_rows(
        {"url": "https://api.polaranalytics.com/file", "format": "csv", "maxRows": 100000},
        session=session,
    )
    assert rows == [{"order_id": "one", "sku": "SALT", "units": "2"}]
    assert len(checksum) == 64
    assert session.get.call_args.kwargs["allow_redirects"] is False
    response.status_code = 302
    with pytest.raises(SourceIncomplete):
        download_report_rows(
            {"url": "https://api.polaranalytics.com/file", "format": "csv", "maxRows": 100000},
            session=session,
        )


def test_full_download_resolves_inline_cut_and_is_still_checked_against_control(monkeypatch):
    from based_inventory import all_channel_sales as sales

    metric = sales.METRICS["TikTok"]
    raw = report(metric, []) | {
        "truncated": True,
        "download": {
            "url": "https://api.polaranalytics.com/file",
            "format": "csv",
            "maxRows": 100000,
        },
    }
    monkeypatch.setattr(
        sales,
        "download_report_rows",
        lambda _: ([{"order_id": "one", "sku": "SALT", metric: "5"}], "b" * 64),
    )
    result = sales.collect_online_source(
        DAY,
        "TikTok",
        Polar(raw, report(metric, [], total=5)),
        ZONE,
        empty_registry(),
        set(),
        {"SALT": "Sea Salt"},
        {},
    )
    assert result["component_units"] == 5
    assert result["transport_sha256"] == "b" * 64


@pytest.mark.parametrize("channel", ["Shopify", "TikTok", "Amazon"])
def test_full_csv_beyond_inline_row_cap_reconciles_and_retains_cutoff_proof(monkeypatch, channel):
    from based_inventory import all_channel_sales as sales

    metric = sales.METRICS[channel]
    main = report(metric, [{"order_id": "preview", "sku": "SALT", metric: 1}]) | {
        "rowLimitReached": {"limit": 5000},
        "tableDataCutForSize": {"inlineRows": 1},
        "truncated": True,
        "download": {
            "url": "https://api.polaranalytics.com/file",
            "format": "csv",
            "maxRows": 100000,
        },
    }
    download = Mock(return_value=([{"order_id": "complete", "sku": "SALT", metric: "5"}], "b" * 64))
    monkeypatch.setattr(sales, "download_report_rows", download)
    result = sales.collect_online_source(
        DAY,
        channel,
        Polar(main, report(metric, [], total=5)),
        ZONE,
        empty_registry(),
        set(),
        {"SALT": "Sea Salt"},
        {},
    )
    download.assert_called_once()
    assert result["listing_units"] == result["control_listing_units"] == 5
    assert result["response_limits"]["rowLimitReached"] == {"limit": 5000}
    assert result["response_limits"]["download_max_rows"] == 100000
    proof = sales.decode_source_proof(result)
    assert proof["response_limits"] == result["response_limits"]
    assert proof["rows"][0]["order_id"] == "complete"
    assert "https://api.polaranalytics.com/file" not in str(proof)
    with pytest.raises(SourceIncomplete, match="control"):
        sales.collect_online_source(
            DAY,
            channel,
            Polar(main, report(metric, [], total=6)),
            ZONE,
            empty_registry(),
            set(),
            {"SALT": "Sea Salt"},
            {},
        )


@pytest.mark.parametrize("max_rows", [5000, 4999, None])
def test_inline_cap_cannot_be_cleared_by_an_equally_capped_or_missing_download(max_rows):
    from based_inventory import all_channel_sales as sales

    metric = sales.METRICS["TikTok"]
    main = report(metric, []) | {"rowLimitReached": {"limit": 5000}}
    if max_rows is not None:
        main["download"] = {
            "url": "https://api.polaranalytics.com/file",
            "format": "csv",
            "maxRows": max_rows,
        }
    with pytest.raises(SourceIncomplete):
        sales.collect_online_source(
            DAY,
            "TikTok",
            Polar(main, report(metric, [], total=0)),
            ZONE,
            empty_registry(),
            set(),
            {"SALT": "Sea Salt"},
            {},
        )


def test_control_row_cap_still_blocks_even_with_a_full_download_descriptor():
    from based_inventory import all_channel_sales as sales

    metric = sales.METRICS["TikTok"]
    main = report(metric, [{"order_id": "one", "sku": "SALT", metric: 5}])
    control = report(metric, [], total=5) | {
        "rowLimitReached": {"limit": 5000},
        "download": {
            "url": "https://api.polaranalytics.com/file",
            "format": "csv",
            "maxRows": 100000,
        },
    }
    with pytest.raises(SourceIncomplete):
        sales.collect_online_source(
            DAY,
            "TikTok",
            Polar(main, control),
            ZONE,
            empty_registry(),
            set(),
            {"SALT": "Sea Salt"},
            {},
        )


def test_sales_day_rejects_corrupted_compressed_proof_and_control_counts():
    from based_inventory.all_channel_sales import SalesDay

    payload = model().payload()
    payload["sources"]["Amazon"]["raw_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        SalesDay.from_payload(payload)
    payload = model().payload()
    payload["sources"]["Amazon"]["control_listing_units"] = 1
    with pytest.raises(ValueError, match="control"):
        SalesDay.from_payload(payload)


def test_full_collector_reads_native_platforms_once_without_warehouse_sales(monkeypatch):
    from based_inventory import all_channel_sales as sales

    class CompletePolar(Polar):
        def context(self):
            return {"context": {"timezone": ZONE}}

        def call(self, tool, payload):
            if tool == "list_dimensions":
                return super().call(tool, payload)
            metric = payload["metrics"]
            return report(metric, [{"order_id": "one", "sku": "BB2000S-1-6-1", metric: 2}])

    parsed = {
        "date": str(DAY),
        "message_id": "message",
        "previous_message_id": "prior",
        "safe_to_write": True,
        "reported_daily_units": 6,
        "derived_daily_units": 6,
        "units_variance": 0,
        "products": [{"label": "SANTAL SEA SALT SPRAY", "sku": "BB2000S-1-6-1", "daily_units": 6}],
    }
    monkeypatch.setattr(sales, "collect_target", lambda *args: parsed)
    shiphero = Mock()
    shiphero.fetch_all_kits.return_value = []
    shiphero.fetch_warehouse_stock.return_value = []
    result = sales.collect_sales_day(
        DAY, CompletePolar({}), shiphero, Mock(), "verified sender query"
    )
    assert result.totals == {"Shopify": 2, "TikTok": 2, "Amazon": 2, "Target": 6}
    assert result.total_units == 12 and result.complete
    assert {row.sku for row in result.rows if row.channel == "Target"} == {"BB2000S-1-6-1-RETAIL"}
    shiphero.fetch_orders_window.assert_not_called()
    shiphero.fetch_all_kits.assert_called_once_with(require_complete=True)


def test_source_proof_survives_roundtrip_without_customer_names_or_email_fields():
    from based_inventory.all_channel_sales import decode_source_proof

    metric = "tiktokshop_orders.raw.quantity_tiktok"
    from based_inventory.all_channel_sales import collect_online_source

    raw = report(
        metric,
        [
            {
                "order_id": "one",
                "sku": "SALT",
                metric: 1,
                "customer_email": "private@example.com",
                "customer_name": "Private",
            }
        ],
    )
    result = collect_online_source(
        DAY, "TikTok", Polar(raw), ZONE, empty_registry(), set(), {"SALT": "Sea Salt"}, {}
    )
    proof = decode_source_proof(result)
    assert proof["rows"][0]["order_id"] == "one"
    assert "customer_email" not in str(proof) and "private@example.com" not in str(proof)


def test_displayed_target_variance_and_report_id_cannot_drift_from_saved_source():
    from based_inventory.all_channel_sales import SalesDay

    for key, value in [("units_variance", 99), ("message_id", "different")]:
        payload = model().payload()
        payload["sources"]["Target"][key] = value
        with pytest.raises(ValueError, match="Target"):
            SalesDay.from_payload(payload)


def test_large_integer_quantities_are_not_rounded_through_binary_float():
    from based_inventory.all_channel_sales import normalize_online_rows

    rows, _, _ = normalize_online_rows(
        [{"order_id": "one", "sku": "SALT", "units": "9007199254740993"}],
        "TikTok",
        empty_registry(),
        set(),
        {"SALT": "Salt"},
        {},
    )
    assert rows[0].units == 9007199254740993


def test_pacific_day_boundary_is_not_assumed_to_be_midnight_utc_or_24_hours():
    from based_inventory.all_channel_sales import _end_of_day

    first = _end_of_day(date(2026, 10, 31), ZONE).astimezone(UTC)
    last = _end_of_day(date(2026, 11, 1), ZONE).astimezone(UTC)
    assert first.hour == 7 and last.hour == 8
    assert last - first == timedelta(hours=25)


def test_download_cap_and_oversized_evidence_fail_closed(monkeypatch):
    from based_inventory import all_channel_sales as sales

    response = Mock(status_code=200)
    response.iter_content.return_value = [b"order_id,sku,units\none,SALT,2\n"]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    session = Mock()
    session.get.return_value = response
    with pytest.raises(SourceIncomplete, match="truncated"):
        sales.download_report_rows(
            {"url": "https://api.polaranalytics.com/file", "format": "csv", "maxRows": 1},
            session=session,
        )
    monkeypatch.setattr(sales, "MAX_RAW_BYTES", 10)
    with pytest.raises(SourceIncomplete, match="size"):
        sales.download_report_rows(
            {"url": "https://api.polaranalytics.com/file", "format": "csv", "maxRows": 100000},
            session=session,
        )


def test_unknown_positive_sku_stays_visible_with_mapping_issue():
    from based_inventory.all_channel_sales import normalize_online_rows

    rows, issues, excluded = normalize_online_rows(
        [{"order_id": "one", "sku": "NEW", "units": 3}], "Shopify", empty_registry(), set(), {}, {}
    )
    assert rows == (SalesRow("NEW", "Unmapped SKU NEW", "Shopify", 3),)
    assert issues == ["Unmapped physical SKU: NEW"] and excluded == 0


def test_flagged_kit_without_recipe_never_becomes_a_single_unit():
    from based_inventory.all_channel_sales import normalize_online_rows

    with pytest.raises(SourceIncomplete, match="bundle"):
        normalize_online_rows(
            [{"order_id": "one", "sku": "KIT", "units": 1}],
            "TikTok",
            empty_registry(),
            {"KIT"},
            {},
            {},
        )


@pytest.mark.parametrize("which", ["report", "control"])
def test_polar_mismatched_integrity_verdict_blocks_sales_even_when_totals_match(which):
    from based_inventory.all_channel_sales import METRICS, collect_online_source

    metric = METRICS["TikTok"]
    main = report(metric, [{"order_id": "one", "sku": "SALT", metric: 5}])
    control = report(metric, [], total=5)
    (main if which == "report" else control)["integrity"] = {
        metric: {"state": "mismatched", "appliesTo": "total", "accuracy": {"matchRate": 85}}
    }
    with pytest.raises(SourceIncomplete, match="integrity"):
        collect_online_source(
            DAY,
            "TikTok",
            Polar(main, control),
            ZONE,
            empty_registry(),
            set(),
            {"SALT": "Sea Salt"},
            {},
        )


def test_partial_integrity_is_retained_as_partial_not_promoted_to_provider_verification():
    from based_inventory.all_channel_sales import (
        METRICS,
        collect_online_source,
        decode_source_proof,
    )

    metric = METRICS["TikTok"]
    main = report(metric, [{"order_id": "one", "sku": "SALT", metric: 5}])
    verdict = {"state": "partial", "appliesTo": "total", "accuracy": {"comparedDays": 0}}
    main["integrity"] = {metric: verdict}
    result = collect_online_source(
        DAY,
        "TikTok",
        Polar(main, report(metric, [], total=5)),
        ZONE,
        empty_registry(),
        set(),
        {"SALT": "Sea Salt"},
        {},
    )
    assert result["component_units"] == 5
    assert result["integrity"] == {"report": verdict}
    assert decode_source_proof(result)["integrity"] == {"report": verdict}


def test_saved_mismatched_integrity_cannot_be_loaded_as_a_complete_capture():
    from based_inventory.all_channel_sales import SalesDay, decode_source_proof, encode_proof

    payload = model().payload()
    source = payload["sources"]["Amazon"]
    integrity = {"report": {"state": "mismatched"}}
    raw = decode_source_proof(source) | {"integrity": integrity}
    source["raw_proof_gzip"], source["raw_sha256"] = encode_proof(raw)
    source["integrity"] = integrity
    with pytest.raises(ValueError, match="integrity"):
        SalesDay.from_payload(payload)
