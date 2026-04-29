"""Tests for weekly_snapshot block construction (ShipHero-sourced)."""

from based_inventory.jobs.weekly_snapshot import ProductLine, build_snapshot_blocks


def test_snapshot_renders_categories() -> None:
    sections = [
        (
            "Hair Care",
            [
                ProductLine(name="Shampoo", qty=3000, sku="BB-SHMP", affected_bundles=[]),
                ProductLine(
                    name="Conditioner",
                    qty=500,
                    sku="BB-COND",
                    affected_bundles=["Shower Duo"],
                ),
            ],
        ),
        (
            "Body",
            [
                ProductLine(
                    name="Body Wash",
                    qty=800,
                    sku="BB-BW",
                    affected_bundles=["Body Care Set", "Shower Essentials"],
                ),
            ],
        ),
    ]

    blocks = build_snapshot_blocks(sections, date_str="Apr 15, 2026")

    assert blocks[0]["type"] == "header"
    assert "Weekly Inventory Audit" in blocks[0]["text"]["text"]

    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert any("*Hair Care*" in t for t in texts)
    assert any("*Body*" in t for t in texts)
    combined = "\n".join(texts)
    assert "3,000" in combined
    assert "500" in combined
    assert "Shower Duo" in combined
    assert "Body Care Set" in combined

    legend = blocks[-1]["elements"][0]["text"]
    assert "5K+" in legend
    assert "Oversold" in legend
    assert "ShipHero" in legend


def test_snapshot_renders_not_found_when_sku_missing() -> None:
    sections = [
        (
            "Skin",
            [ProductLine(name="Tallow Moisturizer", qty=0, sku=None, affected_bundles=[])],
        ),
    ]
    blocks = build_snapshot_blocks(sections, date_str="Apr 15, 2026")
    body_text = "\n".join(
        b["text"]["text"] for b in blocks if b["type"] == "section"
    )
    assert "not found" in body_text


def test_snapshot_emoji_tier_for_oversold() -> None:
    from based_inventory.jobs.weekly_snapshot import _emoji
    assert _emoji(-53) == "⛔"
    assert _emoji(50) == "🚨"
    assert _emoji(500) == "🔴"
    assert _emoji(750) == "🟠"
    assert _emoji(1000) == "🟡"
    assert _emoji(5000) == "📊"
    assert _emoji(50000) == "🟢"
