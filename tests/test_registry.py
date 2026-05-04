"""Tests for the ranked-list name matcher.

`_name_matches` is the resolver primitive that lets weekly_snapshot fall
through filtered candidates instead of bailing on the first kit /
bundle / discontinued hit. The 2026-05-01 'not found in ShipHero'
regression on Scalp Scrubber was caused by the old single-best matcher
returning the kit V1 SKU and giving up.
"""

from based_inventory.registry import _name_match, _name_matches
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


def _index(stocks: list[WarehouseStock]) -> dict[str, list[WarehouseStock]]:
    out: dict[str, list[WarehouseStock]] = {}
    for s in stocks:
        out.setdefault(s.product_name.strip(), []).append(s)
    return out


def test_name_matches_returns_exact_first_then_substring() -> None:
    by_name = _index(
        [
            _stock("KIT-A", "Scalp Scrubber", 67377, is_kit=True),
            _stock("V2", "Scalp Scrubber V2", 68823),
            _stock("DUO", "Scalp Duo", 100),
        ]
    )
    matches = _name_matches("Scalp Scrubber", by_name)
    skus = [m.sku for m in matches]
    # Exact 'Scalp Scrubber' (KIT-A) ranks first even though V2 has higher on_hand:
    # tier-priority dominates within-tier on_hand sort.
    assert skus[0] == "KIT-A"
    # V2 follows via substring fallback so the caller can fall through.
    assert "V2" in skus


def test_name_matches_sorts_within_tier_by_on_hand_desc() -> None:
    by_name = _index(
        [
            _stock("LOW", "Foo Bar", 10),
            _stock("HIGH", "Foo Bar", 1000),
            _stock("MID", "Foo Bar", 500),
        ]
    )
    matches = _name_matches("Foo Bar", by_name)
    assert [m.sku for m in matches] == ["HIGH", "MID", "LOW"]


def test_name_matches_dedup_by_sku() -> None:
    """A SKU matched by exact tier shouldn't reappear via substring tier."""
    by_name = _index([_stock("S1", "Conditioner", 100)])
    matches = _name_matches("Conditioner", by_name)
    skus = [m.sku for m in matches]
    assert skus.count("S1") == 1


def test_name_matches_substring_whole_word_boundary() -> None:
    """Substring matches must be flanked by non-alphanumerics so 'Cream' does
    not match inside 'Creamy' or random brand suffixes."""
    by_name = _index(
        [
            _stock("S1", "Curl Cream", 1000),
            _stock("S2", "Creamy Body Wash", 500),
        ]
    )
    matches = _name_matches("Cream", by_name)
    skus = {m.sku for m in matches}
    # 'Curl Cream' has a space before 'Cream' and end-of-string after, both
    # non-alphanumeric -> match. 'Creamy' fails the after-boundary check.
    assert "S1" in skus
    assert "S2" not in skus


def test_name_matches_short_target_skips_substring_tier() -> None:
    """Targets under 4 chars are too noisy for substring fallback."""
    by_name = _index([_stock("A", "Lotion", 100), _stock("B", "Body Lot Crew", 50)])
    matches = _name_matches("Lot", by_name)
    assert matches == []


def test_name_matches_empty_when_no_match() -> None:
    by_name = _index([_stock("A", "Conditioner", 100)])
    assert _name_matches("Wholly Unrelated", by_name) == []


def test_name_match_is_first_of_name_matches() -> None:
    """`_name_match` should keep the same single-best contract for legacy
    callers (build_registry's bundle component resolution)."""
    by_name = _index(
        [
            _stock("KIT", "Scalp Scrubber", 67377, is_kit=True),
            _stock("V2", "Scalp Scrubber V2", 68823),
        ]
    )
    one = _name_match("Scalp Scrubber", by_name)
    many = _name_matches("Scalp Scrubber", by_name)
    assert one is not None
    assert one.sku == many[0].sku
