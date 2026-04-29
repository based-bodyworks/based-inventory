"""Tests for quantity_alerts job (ShipHero-sourced)."""

from based_inventory.jobs.quantity_alerts import (
    OVERSOLD_LABEL,
    OVERSOLD_TIER,
    Alert,
    _format_cover,
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
    out = _format_channel_mix({
        "BASED": 70,
        "basedbodyworks.myshopify.com": 20,
        "Based Bodyworks Amazon": 10,
    })
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
    blocks = build_blocks(
        [_alert(label=OVERSOLD_LABEL, tier=OVERSOLD_TIER, on_hand=-41)]
    )
    text = blocks[2]["text"]["text"]
    assert "OVERSOLD" in text
    assert "-41" in text
    assert "owe customers" in text


def test_build_blocks_no_velocity_falls_back_to_no_recent_depletion() -> None:
    blocks = build_blocks(
        [_alert(velocity_per_day=0.0, weeks_of_cover=99999.0, on_hand=50)]
    )
    text = blocks[2]["text"]["text"]
    assert "no recent depletion observed" in text
