from datetime import timedelta

import pytest

from based_inventory.pipeline.merchant_publish import build_merchant_plan, validate_plan_readback
from tests.test_pipeline_models import verified


def inputs():
    version = verified()
    serial = (version.day - __import__("datetime").date(1899, 12, 30)).days
    history = [
        [
            "Date",
            "Product",
            "Units",
            "SKU",
            "Channel",
            "Date SKU channel key",
            "Source",
            "Verified on",
        ]
    ]
    for n in range(7):
        day = version.day - timedelta(days=n)
        for ch in ["Shopify", "TIKTOK", "Amazon FBM"]:
            history.append(
                [
                    serial - n,
                    "Product",
                    1,
                    "sku",
                    ch,
                    f"{day}|sku|{ch}",
                    "https://example.com/report",
                    "2026-11-17",
                ]
            )
    rolling = [
        ["Seven-day online ordered physical units"],
        ["Last complete source day", serial],
        ["First source day", serial - 6],
        ["Window days", 7],
        ["Coverage", "Complete"],
        ["Scope", "Seven complete UTC days"],
        [
            "Product",
            "Warehouse SKU",
            "Shopify units",
            "TikTok units",
            "Amazon FBM units",
            "Seven-day units",
            "Units per day",
            "Demand starts",
            "Demand ends",
            "Coverage",
        ],
        ["Product", "sku", 7, 7, 7, 21, 3, serial - 6, serial, "Complete"],
    ]
    daily = [
        [
            "Date",
            "TikTok units",
            "Shopify units",
            "Amazon FBM units",
            "Velocity total",
            "Target units",
            "Target retail sales",
            "Merchant source",
            "Target report",
            "Timing / scope",
        ],
        [serial, 1, 1, 1, 3, 90, 800, "old", "target-report", "Target detail must survive."],
    ]
    return version, history, rolling, daily


def test_one_version_drives_history_daily_and_rolling_without_spill_writes():
    version, history, rolling, daily = inputs()
    result = build_merchant_plan(
        version,
        history,
        rolling,
        daily,
        [["sku", "Product", 1]],
        "https://drive.google.com/file/d/report",
        {},
    )
    assert all(
        r["updateCells"]["start"]["sheetId"] != 2106291003 for r in result["dynamic_requests"]
    )
    changed = result["history_after"][1:4]
    assert [r[2] for r in changed] == [4, 0, 0]
    assert result["daily_after"][1][1:5] == [0, 4, 0, 4]
    assert result["daily_after"][1][5:7] == [90, 800]
    assert "Target detail must survive." in result["daily_after"][1][9]
    assert result["rolling_after"][7][2:6] == [10, 6, 6, 22]


def test_missing_history_day_unknown_sold_sku_and_duplicate_keys_fail_closed():
    version, history, rolling, daily = inputs()
    with pytest.raises(ValueError, match="seven-day"):
        build_merchant_plan(
            version, history[:-3], rolling, daily, [["sku", "Product", 1]], "source", {}
        )
    with pytest.raises(ValueError, match="mapping"):
        build_merchant_plan(version, history, rolling, daily, [], "source", {})
    with pytest.raises(ValueError, match="duplicate"):
        build_merchant_plan(
            version, [*history, history[1]], rolling, daily, [["sku", "Product", 1]], "source", {}
        )


def test_readback_catches_values_that_disagree_with_source():
    version, history, rolling, daily = inputs()
    plan = build_merchant_plan(
        version, history, rolling, daily, [["sku", "Product", 1]], "source", {}
    )
    validate_plan_readback(plan, plan["history_after"], plan["rolling_after"], plan["daily_after"])
    bad = [list(r) for r in plan["history_after"]]
    bad[1][2] = 999
    with pytest.raises(ValueError, match="readback"):
        validate_plan_readback(plan, bad, plan["rolling_after"], plan["daily_after"])


def test_new_day_with_no_orders_marks_all_channels_complete():
    from dataclasses import replace

    version, history, rolling, daily = inputs()
    day_serial = history[1][0]
    history = [history[0], *[r for r in history[1:] if r[0] != day_serial]]
    version = replace(version, rows=())
    result = build_merchant_plan(
        version, history, rolling, daily, [["sku", "Product", 1]], "source", {}
    )
    added = [r for r in result["history_after"][1:] if r[0] == day_serial]
    assert len(added) == 3 and all(r[2] == 0 for r in added)
    assert result["rolling_after"][7][2:6] == [6, 6, 6, 18]


def test_older_day_correction_keeps_current_rolling_window():
    version, history, rolling, daily = inputs()
    today = history[1][0]
    for channel in ("Shopify", "TIKTOK", "Amazon FBM"):
        history.append([today + 1, "Product", 2, "sku", channel, "next", "source", "2026-11-17"])
    rolling[1][1] = today + 1
    result = build_merchant_plan(
        version, history, rolling, daily, [["sku", "Product", 1]], "source", {}
    )
    assert result["rolling_after"][1][1] == today + 1
    assert result["rolling_after"][7][7:9] == [today - 5, today + 1]
