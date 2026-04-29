"""Tests for the live-SKU filter (heuristic + manual list)."""

import json
from pathlib import Path

from based_inventory.discontinued import DiscontinuedFilter, _heuristic_skip


def test_heuristic_skips_test_patterns() -> None:
    assert _heuristic_skip("Shampoo (Test)", "BB-X")
    assert _heuristic_skip("Pomade (Don't Use)", "BB-Y")
    assert _heuristic_skip("Sea Salt Spray (Copy) Just One", "BB-Z")
    assert _heuristic_skip("Tallow Moisturizer (Teasers)", "BB-A")
    assert _heuristic_skip("Body Wash (Review) Santal", "BB-B")
    assert _heuristic_skip("Skincare Restock (Don't Touch)", "BB-C")


def test_heuristic_skips_legacy_versions() -> None:
    assert _heuristic_skip("Based Shampoo 1.0 ", "BB-X")
    assert _heuristic_skip("Based Conditioner 2.0 ", "BB-Y")


def test_heuristic_skips_untitled_prefix() -> None:
    assert _heuristic_skip("Untitled Jun25_22:17 ", "BB-X")
    assert _heuristic_skip("untitled blah", "BB-Y")


def test_heuristic_skips_one_off_products() -> None:
    assert _heuristic_skip("Showerhead Filter ", "BB-X")
    assert _heuristic_skip("Stone Bath Mat White / Mix Pack", "BB-Y")
    assert _heuristic_skip("Based Membership ", "BB-Z")
    assert _heuristic_skip("Troubleshoot Pack of 2", "BB-A")


def test_heuristic_skips_channel_suffixes() -> None:
    assert _heuristic_skip("BASED Rejuvenating Shampoo", "44114434293989-FBA")
    assert _heuristic_skip("Texture Powder Case", "44126606262501-CASE")
    assert _heuristic_skip("Texture Powder Pallet", "44126606262501-PALLET")


def test_heuristic_does_not_skip_real_products() -> None:
    assert not _heuristic_skip("Curl Cream", "BB-CC-SINGLE")
    assert not _heuristic_skip("Shampoo", "44114434293989")
    assert not _heuristic_skip("Body Wash Guava Nectar", "BB-GN-01")
    assert not _heuristic_skip("Under Eye Elixir", "BB-UEE")


def test_manual_discontinued_list_skips(tmp_path: Path) -> None:
    p = tmp_path / "discontinued.json"
    p.write_text(json.dumps({
        "skus": [
            {"sku": "TLLW-1", "name": "Tallow Moisturizer", "reason": "EOL"},
        ]
    }))
    f = DiscontinuedFilter(p)
    assert f.should_skip("TLLW-1", "Tallow Moisturizer")
    # Real product not in the list passes through.
    assert not f.should_skip("BB-CC-SINGLE", "Curl Cream")


def test_missing_discontinued_file_is_safe(tmp_path: Path) -> None:
    f = DiscontinuedFilter(tmp_path / "does-not-exist.json")
    # Heuristic still applies even when manual file missing.
    assert f.should_skip("44114434293989-FBA", "BASED Rejuvenating Shampoo")
    assert not f.should_skip("BB-CC-SINGLE", "Curl Cream")
