"""Tests for quantity_alerts job (ShipHero-sourced)."""

from based_inventory.jobs.quantity_alerts import (
    OVERSOLD_LABEL,
    OVERSOLD_TIER,
    Alert,
    _availability_tier,
    _backorder_tier_for,
    _format_cover,
    _severity_rank,
    _tier_for,
    build_blocks,
)


def test_tier_for_oversold() -> None:
    tier, label = _tier_for(-53)
    assert tier == OVERSOLD_TIER
    assert label == OVERSOLD_LABEL


def test_tier_for_critical_low_warning_headsup() -> None:
    assert _tier_for(50) == (100, "🚨 CRITICAL")
    assert _tier_for(100) == (100, "🚨 CRITICAL")
    assert _tier_for(101) == (500, "🔴 LOW STOCK")
    assert _tier_for(500) == (500, "🔴 LOW STOCK")
    assert _tier_for(750) == (750, "🟠 WARNING")
    assert _tier_for(1000) == (1000, "🟡 HEADS UP")


def test_tier_for_above_threshold_returns_none() -> None:
    assert _tier_for(1001) is None
    assert _tier_for(50_000) is None


def test_format_cover_subweek_uses_2_decimals() -> None:
    assert _format_cover(0.04) == "0.04w"
    assert _format_cover(0.4) == "0.40w"
    assert _format_cover(1.0) == "1.0w"
    assert _format_cover(8.5) == "8.5w"
    assert _format_cover(99999.0) == "∞ (no observed depletion)"


def _alert(**overrides) -> Alert:
    base = dict(
        label="🚨 CRITICAL",
        tier=100,
        sku="BB-SHMP",
        product_name="Shampoo",
        on_hand=50,
        velocity_per_day=156.0,
        weeks_of_cover=0.05,
        affected_bundles=["Shower Duo", "Shower Essentials"],
        inbound_outstanding=0,
        inbound_po_count=0,
        inbound_latest_po_date=None,
        inbound_latest_ship_date=None,
        available=50,  # default matches on_hand so existing tests don't show "available" line
        backorder=0,
        backorder_label=None,
    )
    base.update(overrides)
    return Alert(**base)


def test_build_blocks_renders_inbound_when_present() -> None:
    blocks = build_blocks(
        [
            _alert(
                inbound_outstanding=7500,
                inbound_po_count=2,
                inbound_latest_po_date="2026-04-21T12:00:00",
                inbound_latest_ship_date=None,
            )
        ]
    )
    text = blocks[2]["text"]["text"]
    assert "7,500" in text
    assert "2 pending POs" in text
    assert "no ship_date" in text or "2026-04-21" in text


def test_build_blocks_renders_inbound_with_ship_date_when_set() -> None:
    blocks = build_blocks(
        [
            _alert(
                inbound_outstanding=3000,
                inbound_po_count=1,
                inbound_latest_po_date="2026-04-20T12:00:00",
                inbound_latest_ship_date="2026-04-29T08:00:00",
            )
        ]
    )
    text = blocks[2]["text"]["text"]
    assert "3,000" in text
    assert "1 pending PO" in text
    assert "2026-04-29" in text


def test_build_blocks_omits_inbound_when_zero() -> None:
    blocks = build_blocks([_alert()])
    text = blocks[2]["text"]["text"]
    assert "📥" not in text
    assert "pending PO" not in text


def test_format_channel_mix_renders_known_channel_labels() -> None:
    from based_inventory.jobs.quantity_alerts import _format_channel_mix

    out = _format_channel_mix(
        {
            "BASED": 70,
            "basedbodyworks.myshopify.com": 20,
            "Based Bodyworks Amazon": 10,
        }
    )
    assert out is not None
    assert "TTS 70%" in out
    assert "Shopify 20%" in out
    assert "Amazon 10%" in out


def test_format_channel_mix_returns_none_for_empty() -> None:
    from based_inventory.jobs.quantity_alerts import _format_channel_mix

    assert _format_channel_mix({}) is None


def test_build_blocks_appends_channel_mix_to_footer() -> None:
    blocks = build_blocks([_alert()], channel_mix_summary="TTS 65% / Shopify 25% / Amazon 10%")
    footer = blocks[-1]["elements"][0]["text"]
    assert "TTS 65%" in footer
    assert "channel mix" in footer.lower()


def test_build_blocks_renders_critical_alert() -> None:
    blocks = build_blocks([_alert()])
    assert blocks[0]["type"] == "header"
    assert "Inventory Alert" in blocks[0]["text"]["text"]
    assert blocks[1]["type"] == "divider"
    body = blocks[2]
    assert body["type"] == "section"
    text = body["text"]["text"]
    assert "CRITICAL" in text
    assert "Shampoo" in text
    assert "50" in text
    assert "0.05w" in text
    assert "156" in text
    assert "Shower Duo" in text
    footer = blocks[-1]
    assert footer["type"] == "context"
    assert "ShipHero" in footer["elements"][0]["text"]


def test_build_blocks_oversold_includes_owe_message() -> None:
    blocks = build_blocks([_alert(label=OVERSOLD_LABEL, tier=OVERSOLD_TIER, on_hand=-41)])
    text = blocks[2]["text"]["text"]
    assert "OVERSOLD" in text
    assert "-41" in text
    assert "owe customers" in text


def test_build_blocks_no_velocity_falls_back_to_no_recent_depletion() -> None:
    blocks = build_blocks([_alert(velocity_per_day=0.0, weeks_of_cover=99999.0, on_hand=50)])
    text = blocks[2]["text"]["text"]
    assert "no recent depletion observed" in text


# --------------------------------------------------------------------------
# Velocity interpretation: in-stock burst rate vs 7d average annotation
# --------------------------------------------------------------------------


def test_format_sample_window_renders_days_hours_or_minutes() -> None:
    from based_inventory.jobs.quantity_alerts import _format_sample_window

    assert _format_sample_window(2.5) == "2.5d"
    assert _format_sample_window(0.5) == "12.0h"
    assert _format_sample_window(0.04) == "57.6min" or _format_sample_window(0.04).endswith("min")


def test_velocity_interpretation_returns_none_for_full_window() -> None:
    from based_inventory.jobs.quantity_alerts import _velocity_interpretation

    # If the sample window matches the requested window, no annotation needed.
    assert _velocity_interpretation(2000, 7.0, 7) is None
    assert _velocity_interpretation(2000, 6.7, 7) is None  # within 95% threshold


def test_velocity_interpretation_surfaces_sample_context_without_calendar_avg() -> None:
    from based_inventory.jobs.quantity_alerts import _velocity_interpretation

    # Saturated case: 2,377 units captured in 0.3 days -> short sample window.
    annotation = _velocity_interpretation(2377, 0.3, 7)
    assert annotation is not None
    assert "2,377 units" in annotation
    assert "shipped last 7d" in annotation
    # Calendar-avg framing was removed 2026-05-08 (operationally misleading).
    assert "calendar avg" not in annotation
    assert "calendar" not in annotation
    # Sample window rendered in hours/minutes for short spans.
    assert "h " in annotation or "min " in annotation
    assert "In-stock rate sampled" in annotation


def test_build_blocks_renders_burst_rate_label_when_window_short() -> None:
    blocks = build_blocks(
        [
            _alert(
                velocity_per_day=1667.0,
                weeks_of_cover=0.0,
                depletion_units=2377,
                effective_window_days=0.3,
            )
        ]
    )
    text = blocks[2]["text"]["text"]
    assert "1,667/day in-stock rate" in text
    assert "2,377 units shipped last 7d" in text
    assert "In-stock rate sampled" in text
    assert "calendar avg" not in text


def test_build_blocks_renders_normal_velocity_label_when_window_full() -> None:
    blocks = build_blocks(
        [
            _alert(
                velocity_per_day=156.0,
                weeks_of_cover=0.05,
                depletion_units=1092,
                effective_window_days=7.0,
            )
        ]
    )
    text = blocks[2]["text"]["text"]
    # Full-window: should label as "velocity" not "in-stock rate"
    assert "/day velocity" in text
    assert "in-stock rate" not in text
    # No burst-rate annotation because the sample matched the requested window
    assert "Burst rate sampled" not in text


def test_build_blocks_renders_fba_quantity_when_present() -> None:
    blocks = build_blocks([_alert(fba_quantity=2350)])
    text = blocks[2]["text"]["text"]
    assert "Amazon FBA on-hand" in text
    assert "2,350" in text


def test_build_blocks_omits_fba_when_no_record() -> None:
    blocks = build_blocks([_alert(fba_quantity=None)])
    text = blocks[2]["text"]["text"]
    assert "Amazon FBA" not in text


# --------------------------------------------------------------------------
# Dual-ladder: availability (uses `available` for non-OVERSOLD) + backorder.
# 2026-05-20: prior on_hand-only logic missed the CLAY1 case where physical
# stock (312) hid the fact that all units were allocated (available=0) and
# 14,796 units were backordered. These tests pin the corrected behavior.
# --------------------------------------------------------------------------


def test_availability_tier_uses_oversold_for_negative_on_hand() -> None:
    # Physical negative trumps available; this is the "we owe customers" state.
    assert _availability_tier(on_hand=-53, available=0) == (OVERSOLD_TIER, OVERSOLD_LABEL)


def test_availability_tier_uses_available_when_on_hand_nonneg() -> None:
    # The CLAY1 case: on_hand=312 (looks like LOW STOCK), but available=0
    # because every unit is allocated. We want CRITICAL, not LOW STOCK.
    assert _availability_tier(on_hand=312, available=0) == (100, "🚨 CRITICAL")
    # Ditto: lots of physical stock, all promised.
    assert _availability_tier(on_hand=10_000, available=50) == (100, "🚨 CRITICAL")
    # Honest healthy state: available high enough to clear top bucket.
    assert _availability_tier(on_hand=5_000, available=5_000) is None


def test_backorder_tier_for_buckets() -> None:
    assert _backorder_tier_for(0) is None
    assert _backorder_tier_for(99) is None  # below floor
    assert _backorder_tier_for(100) == (100, "📥 BACKORDER NOTICE")
    assert _backorder_tier_for(999) == (100, "📥 BACKORDER NOTICE")
    assert _backorder_tier_for(1_000) == (1_000, "📥 BACKORDER ALARM")
    assert _backorder_tier_for(4_999) == (1_000, "📥 BACKORDER ALARM")
    assert _backorder_tier_for(5_000) == (5_000, "📥📥 BACKORDER CRITICAL")
    assert _backorder_tier_for(9_999) == (5_000, "📥📥 BACKORDER CRITICAL")
    assert _backorder_tier_for(10_000) == (10_000, "📥📥📥 BACKORDER MASSIVE")
    assert _backorder_tier_for(14_796) == (10_000, "📥📥📥 BACKORDER MASSIVE")  # CLAY1
    assert _backorder_tier_for(50_000) == (10_000, "📥📥📥 BACKORDER MASSIVE")


def test_severity_rank_oversold_pins_top_regardless_of_backorder() -> None:
    # OVERSOLD (rank 0) is worse than any backorder bucket.
    rank, label = _severity_rank(avail_tier=OVERSOLD_TIER, backorder_tier=10_000)
    assert rank == 0
    assert label == OVERSOLD_LABEL


def test_severity_rank_backorder_massive_beats_low_stock() -> None:
    # A 10K+ backorder hole is worse than LOW STOCK (avail tier 500).
    rank, label = _severity_rank(avail_tier=500, backorder_tier=10_000)
    assert label == "📥📥📥 BACKORDER MASSIVE"
    assert rank == 150


def test_severity_rank_critical_beats_backorder_massive() -> None:
    # Availability CRITICAL (rank 100) still leads over MASSIVE backorder (150);
    # the unit-zero state is more acute than the demand-queue size.
    rank, label = _severity_rank(avail_tier=100, backorder_tier=10_000)
    assert rank == 100
    assert label == "🚨 CRITICAL"


def test_severity_rank_only_backorder_fires() -> None:
    # Availability healthy (above HEADS UP), but backorder crossed a bucket.
    rank, label = _severity_rank(avail_tier=None, backorder_tier=5_000)
    assert rank == 250
    assert label == "📥📥 BACKORDER CRITICAL"


def test_severity_rank_only_availability_fires() -> None:
    rank, label = _severity_rank(avail_tier=500, backorder_tier=None)
    assert rank == 200
    assert label == "🔴 LOW STOCK"


def test_build_blocks_renders_available_when_differs_from_on_hand() -> None:
    # CLAY1: 312 on_hand, 0 available, 14,796 backordered → must surface all three.
    blocks = build_blocks(
        [
            _alert(
                label="🚨 CRITICAL",
                tier=100,
                sku="CLAY1",
                product_name="Hair Clay",
                on_hand=312,
                available=0,
                backorder=14_796,
                backorder_label="📥📥📥 BACKORDER MASSIVE",
                affected_bundles=["Complete Styling Kit"],
            )
        ]
    )
    text = blocks[2]["text"]["text"]
    assert "312" in text and "on hand" in text
    assert "0" in text and "available" in text
    assert "14,796" in text and "backordered" in text
    # The backorder ladder triggered too; secondary label should appear.
    assert "BACKORDER MASSIVE" in text


def test_build_blocks_omits_available_when_matches_on_hand() -> None:
    # Healthy-ish SKU where on_hand == available: don't add a redundant line.
    blocks = build_blocks([_alert(on_hand=400, available=400, backorder=0)])
    text = blocks[2]["text"]["text"]
    assert "on hand" in text
    assert "available" not in text  # no parenthetical redundancy
    assert "backordered" not in text


def test_build_blocks_does_not_repeat_backorder_label_when_primary() -> None:
    # When the backorder ladder owns the primary label, we don't want it
    # echoed on a second line; build_blocks suppresses the secondary echo.
    blocks = build_blocks(
        [
            _alert(
                label="📥📥📥 BACKORDER MASSIVE",
                tier=150,
                on_hand=2_000,
                available=2_000,
                backorder=12_000,
                backorder_label="📥📥📥 BACKORDER MASSIVE",
            )
        ]
    )
    text = blocks[2]["text"]["text"]
    # Should only appear once (in the header line), not duplicated as a secondary line.
    assert text.count("BACKORDER MASSIVE") == 1
