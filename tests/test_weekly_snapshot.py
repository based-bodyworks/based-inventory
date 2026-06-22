"""Tests for weekly_snapshot block construction (ShipHero-sourced)."""

from __future__ import annotations

import json
from pathlib import Path

from based_inventory.discontinued import DiscontinuedFilter
from based_inventory.jobs.weekly_snapshot import (
    ProductLine,
    Resolved,
    _load_aliases,
    _resolve_to_stock,
    build_snapshot_blocks,
)
from based_inventory.shiphero import WarehouseStock


def _stock(
    sku: str,
    name: str,
    on_hand: int,
    is_kit: bool = False,
    available: int | None = None,
    backorder: int = 0,
) -> WarehouseStock:
    return WarehouseStock(
        sku=sku,
        on_hand=on_hand,
        available=on_hand if available is None else available,
        allocated=0 if available is None else max(0, on_hand - available),
        backorder=backorder,
        reserve_inventory=0,
        sell_ahead=0,
        product_name=name,
        is_kit=is_kit,
    )


def _index(stocks: list[WarehouseStock]) -> tuple[dict, dict]:
    by_name: dict[str, list[WarehouseStock]] = {}
    by_sku: dict[str, WarehouseStock] = {}
    for s in stocks:
        by_name.setdefault(s.product_name.strip(), []).append(s)
        by_sku[s.sku] = s
    return by_name, by_sku


def _empty_disc() -> DiscontinuedFilter:
    return DiscontinuedFilter(Path("/nonexistent/discontinued.json"))


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
    # Bundle list is no longer appended inline (audit table is a snapshot,
    # not an alert; per-row bundle bleed was making rows multi-line ugly).
    # Bundles are still tracked on the dataclass for any future surface
    # that wants them; they're just not rendered into the Slack table.
    assert "Shower Duo" not in combined
    assert "Body Care Set" not in combined

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
    body_text = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    assert "not found" in body_text


def test_snapshot_emoji_tier_for_oversold() -> None:
    """Legacy ladder test. The signature changed in 2026-05-08 from
    `_emoji(qty)` (where qty meant on_hand) to `_emoji(available, backorder,
    on_hand)`. With the new contract, ⛔ requires explicit on_hand < 0; a
    negative value passed as `available` falls through to the available
    ladder (it's not the right input to express physical-oversold)."""
    from based_inventory.jobs.weekly_snapshot import _emoji

    assert _emoji(available=0, on_hand=-53) == "⛔"
    assert _emoji(available=50) == "🚨"
    assert _emoji(available=500) == "🔴"
    assert _emoji(available=750) == "🟠"
    assert _emoji(available=1000) == "🟡"
    assert _emoji(available=5000) == "📊"
    assert _emoji(available=50000) == "🟢"


# Resolver fallback + alias regression tests.
# Five real cases observed in the 2026-05-01 snapshot post:
#   "Hair Clay", "Leave-In Conditioner", "Tallow Moisturizer",
#   "Scalp Scrubber", "Wooden Hair Comb" all rendered as
#   "not found in ShipHero" despite live trusted-single SKUs existing.


def test_resolver_falls_back_when_top_match_is_kit() -> None:
    """Scalp Scrubber regression: legacy V1 'Scalp Scrubber' is mis-flagged
    is_kit=True. Without fallback the lookup bails to None and the post
    shows 'not found' even though V2 single is right behind it."""
    stocks = [
        _stock("BB-ACCS-SCPS", "Scalp Scrubber", 67377, is_kit=True),
        _stock("BB-ACCS-SCLPSCRBR-V2", "Scalp Scrubber V2", 68823),
    ]
    by_name, by_sku = _index(stocks)
    resolved = _resolve_to_stock(
        "Scalp Scrubber", by_name, by_sku, frozenset(), _empty_disc(), aliases={}
    )
    assert resolved is not None
    assert resolved.primary_sku == "BB-ACCS-SCLPSCRBR-V2"
    assert resolved.qty == 68823


def test_resolver_falls_back_when_top_match_is_bundle() -> None:
    """When the highest-on_hand fuzzy match is a registry-known bundle SKU,
    skip it and try the next candidate."""
    stocks = [
        _stock("BUNDLE-X", "Curl Cream Bundle", 50000),
        _stock("BB-CRMC", "Curl Cream", 7965),
    ]
    by_name, by_sku = _index(stocks)
    resolved = _resolve_to_stock(
        "Curl Cream",
        by_name,
        by_sku,
        bundle_skus=frozenset({"BUNDLE-X"}),
        discontinued=_empty_disc(),
        aliases={},
    )
    assert resolved is not None
    assert resolved.primary_sku == "BB-CRMC"


def test_alias_pins_to_specific_sku() -> None:
    """Hair Clay regression: ShipHero canonical name is 'Clay'. Substring
    fallback can't bridge the rename, so the alias pins it directly."""
    stocks = [
        _stock("CLAYSC", "Hair Clay Deluxe Bundle", 7855, is_kit=True),
        _stock("CLAY1", "Clay", 13326),
    ]
    by_name, by_sku = _index(stocks)
    aliases = {"Hair Clay": {"sku": "CLAY1"}}
    resolved = _resolve_to_stock(
        "Hair Clay", by_name, by_sku, frozenset(), _empty_disc(), aliases=aliases
    )
    assert resolved is not None
    assert resolved.primary_sku == "CLAY1"
    assert resolved.qty == 13326


def test_alias_aggregates_across_skus() -> None:
    """Tallow Moisturizer ships in 50ml + 100ml variants; the audit layout
    treats it as one product. Alias sums on_hand across both SKUs."""
    stocks = [
        _stock("BB-ONE-BTAL-50ML", "Tallow Moisturizer 50ml", 120),
        _stock("BB-ONE-BTAL-100ML", "Tallow 100ml", 80),
    ]
    by_name, by_sku = _index(stocks)
    aliases = {"Tallow Moisturizer": {"skus": ["BB-ONE-BTAL-50ML", "BB-ONE-BTAL-100ML"]}}
    resolved = _resolve_to_stock(
        "Tallow Moisturizer", by_name, by_sku, frozenset(), _empty_disc(), aliases=aliases
    )
    assert resolved is not None
    assert resolved.qty == 200
    assert set(resolved.skus) == {"BB-ONE-BTAL-50ML", "BB-ONE-BTAL-100ML"}
    # Primary is the highest-on_hand contributor (label/UI tiebreak).
    assert resolved.primary_sku == "BB-ONE-BTAL-50ML"


def test_alias_falls_through_when_skus_missing_from_warehouse() -> None:
    """If an alias points to SKUs that aren't in this warehouse's stock,
    don't silently report 0; fall through to fuzzy match."""
    stocks = [_stock("BB-LEAVEIN-ONE", "Leave In Cond", 18934)]
    by_name, by_sku = _index(stocks)
    aliases = {"Leave-In Conditioner": {"sku": "MISSING-SKU"}}
    resolved = _resolve_to_stock(
        "Leave-In Conditioner",
        by_name,
        by_sku,
        frozenset(),
        _empty_disc(),
        aliases=aliases,
    )
    # Fuzzy match won't find "Leave-In Conditioner" in "Leave In Cond" either
    # (different punctuation + truncation), so this returns None — the
    # important behavior is that the resolver tried fuzzy, didn't fabricate 0.
    assert resolved is None


def test_resolver_returns_none_when_all_candidates_filtered() -> None:
    stocks = [
        _stock("KIT-A", "Foo Bar Kit", 1000, is_kit=True),
        _stock("KIT-B", "Foo Bar Pack", 500, is_kit=True),
    ]
    by_name, by_sku = _index(stocks)
    resolved = _resolve_to_stock("Foo Bar", by_name, by_sku, frozenset(), _empty_disc(), aliases={})
    assert resolved is None


def test_load_aliases_handles_missing_file(tmp_path: Path) -> None:
    assert _load_aliases(tmp_path / "does-not-exist.json") == {}


def test_load_aliases_handles_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{ not valid json")
    assert _load_aliases(p) == {}


def test_load_aliases_parses_valid_file(tmp_path: Path) -> None:
    p = tmp_path / "aliases.json"
    p.write_text(json.dumps({"aliases": {"X": {"sku": "S1"}, "Y": {"skus": ["S2", "S3"]}}}))
    out = _load_aliases(p)
    assert out == {"X": {"sku": "S1"}, "Y": {"skus": ["S2", "S3"]}}


def test_resolved_dataclass_fields() -> None:
    r = Resolved(primary_sku="X", on_hand=10, skus=("X",))
    assert r.primary_sku == "X"
    assert r.on_hand == 10
    assert r.qty == 10  # back-compat alias
    assert r.skus == ("X",)
    # New fields default to 0; available defaults explicitly (not on_hand)
    # since callers may legitimately want to express "0 sellable" via Resolved.
    assert r.available == 0
    assert r.backorder == 0


def test_resolved_carries_available_and_backorder() -> None:
    r = Resolved(primary_sku="X", on_hand=4724, available=0, backorder=6080, skus=("X",))
    assert r.on_hand == 4724
    assert r.available == 0
    assert r.backorder == 6080


def test_resolve_pulls_available_and_backorder_from_warehouse_stock() -> None:
    """The conditioner case: 4,724 on_hand with 0 available + 6,080 backordered.
    Before this fix, the snapshot rendered '4,724' and looked healthy. After,
    the resolver carries all three numbers downstream."""
    stocks = [_stock("BB-COND", "Conditioner", 4724, available=0, backorder=6080)]
    by_name, by_sku = _index(stocks)
    resolved = _resolve_to_stock(
        "Conditioner", by_name, by_sku, frozenset(), _empty_disc(), aliases={}
    )
    assert resolved is not None
    assert resolved.on_hand == 4724
    assert resolved.available == 0
    assert resolved.backorder == 6080


def test_resolve_alias_sums_available_and_backorder_across_skus() -> None:
    """For multi-SKU aliases (Tallow 50ml + 100ml), all three quantity
    fields must be summed, not just on_hand."""
    stocks = [
        _stock("BB-ONE-BTAL-50ML", "Tallow Moisturizer 50ml", 120, available=100, backorder=20),
        _stock("BB-ONE-BTAL-100ML", "Tallow 100ml", 80, available=50, backorder=30),
    ]
    by_name, by_sku = _index(stocks)
    aliases = {"Tallow Moisturizer": {"skus": ["BB-ONE-BTAL-50ML", "BB-ONE-BTAL-100ML"]}}
    resolved = _resolve_to_stock(
        "Tallow Moisturizer", by_name, by_sku, frozenset(), _empty_disc(), aliases=aliases
    )
    assert resolved is not None
    assert resolved.on_hand == 200
    assert resolved.available == 150
    assert resolved.backorder == 50


# --------------------------------------------------------------------------
# Render: healthy compact vs. at-risk expanded
# --------------------------------------------------------------------------


def test_render_healthy_sku_shows_compact_available_only() -> None:
    from based_inventory.jobs.weekly_snapshot import _render_line

    line = ProductLine(
        name="Shampoo",
        on_hand=73932,
        available=73932,
        backorder=0,
        sku="BB-SHMP",
        affected_bundles=[],
    )
    out = _render_line(line)
    assert out == "🟢 Shampoo: *73,932* available"
    assert "on hand" not in out
    assert "backordered" not in out


def test_render_at_risk_sku_shows_full_picture() -> None:
    """The headline regression case: Conditioner at 4,724 on_hand, 0
    available, 6,080 backordered should render as a CRITICAL alert with
    all three numbers visible — not as 'Conditioner: 4,724' which looks
    healthy."""
    from based_inventory.jobs.weekly_snapshot import _render_line

    line = ProductLine(
        name="Conditioner",
        on_hand=4724,
        available=0,
        backorder=6080,
        sku="BB-COND",
        affected_bundles=[],
    )
    out = _render_line(line)
    assert out.startswith("🚨")  # available=0 triggers critical, not 📊 1K-5K
    assert "4,724" in out
    assert "0" in out
    assert "6,080" in out
    assert "on hand" in out
    assert "available" in out
    assert "backordered" in out


def test_render_backorder_with_healthy_available_triggers_expanded() -> None:
    """Healthy available but a backorder queue still warrants the expanded
    format so the queue is visible. Emoji stays healthy (the queue isn't
    big enough to dent sellable stock) but the line is no longer compact."""
    from based_inventory.jobs.weekly_snapshot import _render_line

    line = ProductLine(
        name="Shampoo",
        on_hand=30000,
        available=30000,
        backorder=124,
        sku="BB-SHMP",
        affected_bundles=[],
    )
    out = _render_line(line)
    assert "on hand" in out
    assert "available" in out
    assert "backordered" in out
    assert "124" in out


def test_render_emoji_tier_picks_from_available_not_on_hand() -> None:
    """Regression guard: a SKU with 4,724 on_hand but 0 available must NOT
    render with the '📊 1K-5K' healthy band emoji. It should be 🚨 because
    available <= 100."""
    from based_inventory.jobs.weekly_snapshot import _emoji

    assert _emoji(available=0, backorder=6080) == "🚨"
    # on_hand is irrelevant to the band — the function shouldn't be tricked
    # into thinking high on_hand means healthy.
    assert _emoji(available=0, backorder=6080, on_hand=4724) == "🚨"
    # Oversold sentinel still fires when on_hand goes physical-negative.
    assert _emoji(available=0, on_hand=-15) == "⛔"
    # available alone, no backorder: standard ladder.
    assert _emoji(available=50000) == "🟢"
    assert _emoji(available=2000) == "📊"
    assert _emoji(available=900) == "🟡"
    assert _emoji(available=600) == "🟠"
    assert _emoji(available=300) == "🔴"
    assert _emoji(available=50) == "🚨"


def test_render_preserves_fba_qty_annotation_in_both_formats() -> None:
    from based_inventory.jobs.weekly_snapshot import _render_line

    healthy = ProductLine(
        name="Shampoo", on_hand=50000, available=50000, sku="BB-SHMP",
        affected_bundles=[], fba_qty=1200,
    )
    risky = ProductLine(
        name="Conditioner", on_hand=4724, available=0, backorder=6080,
        sku="BB-COND", affected_bundles=[], fba_qty=0,
    )
    assert "🅰️" in _render_line(healthy)
    assert "1,200" in _render_line(healthy)
    assert "🅰️" in _render_line(risky)


def test_render_fetch_error_takes_precedence() -> None:
    from based_inventory.jobs.weekly_snapshot import _render_line

    line = ProductLine(name="Hair Clay", on_hand=0, sku=None, affected_bundles=[], fetch_error=True)
    out = _render_line(line)
    assert "lookup failed" in out
    assert "retry" in out


def test_snapshot_legend_explains_available_vs_on_hand() -> None:
    """The legend must teach readers that the emoji ladder is driven by
    `available` (sellable) and that at-risk rows expand to show all three
    numbers. Without that text, a reader sees 'Conditioner: 4,724 on hand
    · 0 available · 6,080 backordered' and may not understand why the
    headline number isn't first."""
    blocks = build_snapshot_blocks([], date_str="May 8, 2026")
    legend = blocks[-1]["elements"][0]["text"]
    assert "available" in legend
    assert "sellable" in legend.lower()
    assert "backordered" in legend


def test_shipped_audit_aliases_file_is_valid() -> None:
    """Sanity-check the actual audit-aliases.json shipped in data/.

    Catches accidental schema drift (e.g., someone editing the file by
    hand and producing invalid JSON or an entry without sku/skus).
    """
    repo_root = Path(__file__).resolve().parents[1]
    aliases = _load_aliases(repo_root / "data" / "audit-aliases.json")
    assert aliases, "audit-aliases.json should ship with the 5 known overrides"
    for name, entry in aliases.items():
        has_sku = "sku" in entry and isinstance(entry["sku"], str)
        has_skus = "skus" in entry and isinstance(entry["skus"], list) and entry["skus"]
        assert has_sku or has_skus, f"alias {name!r} needs 'sku' or non-empty 'skus'"
