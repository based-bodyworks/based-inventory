"""Tests for weekly_snapshot block construction (ShipHero-sourced)."""

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


def _stock(sku: str, name: str, on_hand: int, is_kit: bool = False) -> WarehouseStock:
    return WarehouseStock(
        sku=sku,
        on_hand=on_hand,
        available=on_hand,
        allocated=0,
        backorder=0,
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
    body_text = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
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
    aliases = {
        "Tallow Moisturizer": {"skus": ["BB-ONE-BTAL-50ML", "BB-ONE-BTAL-100ML"]}
    }
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
    resolved = _resolve_to_stock(
        "Foo Bar", by_name, by_sku, frozenset(), _empty_disc(), aliases={}
    )
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
    r = Resolved(primary_sku="X", qty=10, skus=("X",))
    assert r.primary_sku == "X"
    assert r.qty == 10
    assert r.skus == ("X",)


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
