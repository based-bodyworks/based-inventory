"""Physical demand from direct, pre-expanded, and nested ShipHero kits."""

from __future__ import annotations

import pytest

from based_inventory.inventory import DemandSample, aggregate_orders_demand
from based_inventory.registry import BundleEntry, BundleRegistry


def _entry(sku: str, *components: tuple[str | None, int]) -> BundleEntry:
    return BundleEntry(
        bundle_sku=sku,
        bundle_name=sku,
        source="shiphero",
        components_resolved=tuple(
            (component, component or "unknown", qty) for component, qty in components
        ),
        partially_resolved=any(component is None for component, _qty in components),
    )


def _registry(*entries: BundleEntry) -> BundleRegistry:
    by_sku = {entry.bundle_sku: entry for entry in entries}
    return BundleRegistry(
        bundles=entries,
        bundle_skus=frozenset(by_sku),
        by_bundle_sku=by_sku,
    )


def _order(*lines: tuple[str, int]) -> dict:
    return {
        "line_items": {"edges": [{"node": {"sku": sku, "quantity": qty}} for sku, qty in lines]}
    }


CHILD = _entry("CHILD", ("SHAMPOO", 1), ("COMB", 1))
PARENT = _entry("PARENT", ("CHILD", 1), ("TOWEL", 1))
NESTED = _registry(CHILD, PARENT)


@pytest.mark.parametrize(
    "order",
    [
        _order(("PARENT", 1), ("CHILD", 1), ("SHAMPOO", 1), ("COMB", 1), ("TOWEL", 1)),
        _order(("CHILD", 1), ("COMB", 1), ("PARENT", 1), ("TOWEL", 1), ("SHAMPOO", 1)),
    ],
)
def test_nested_markers_with_preexpanded_leaves_are_not_double_counted(order: dict) -> None:
    assert aggregate_orders_demand([order], NESTED) == {
        "SHAMPOO": DemandSample(units=1, order_count=1),
        "COMB": DemandSample(units=1, order_count=1),
        "TOWEL": DemandSample(units=1, order_count=1),
    }


def test_nested_parent_and_child_markers_without_leaves_expand_once() -> None:
    # ShipHero can list both kit markers for one parent sale. The child marker
    # is structural here, not proof of a second separately purchased kit.
    assert aggregate_orders_demand([_order(("CHILD", 1), ("PARENT", 1))], NESTED) == {
        "SHAMPOO": DemandSample(units=1, order_count=1),
        "COMB": DemandSample(units=1, order_count=1),
        "TOWEL": DemandSample(units=1, order_count=1),
    }


def test_standalone_nested_parent_expands_to_physical_leaves() -> None:
    assert aggregate_orders_demand([_order(("PARENT", 2))], NESTED) == {
        "SHAMPOO": DemandSample(units=2, order_count=1),
        "COMB": DemandSample(units=2, order_count=1),
        "TOWEL": DemandSample(units=2, order_count=1),
    }


def test_parent_with_preexpanded_leaves_and_no_child_marker_is_not_double_counted() -> None:
    order = _order(("PARENT", 1), ("SHAMPOO", 1), ("COMB", 1), ("TOWEL", 1))
    assert aggregate_orders_demand([order], NESTED) == {
        "SHAMPOO": DemandSample(units=1, order_count=1),
        "COMB": DemandSample(units=1, order_count=1),
        "TOWEL": DemandSample(units=1, order_count=1),
    }


def test_parent_before_child_at_any_nesting_depth() -> None:
    registry = _registry(
        _entry("GRANDCHILD", ("COMB", 1)),
        _entry("CHILD", ("GRANDCHILD", 1)),
        _entry("PARENT", ("CHILD", 1)),
    )
    order = _order(("GRANDCHILD", 1), ("CHILD", 1), ("PARENT", 1))
    assert aggregate_orders_demand([order], registry) == {
        "COMB": DemandSample(units=1, order_count=1)
    }


def test_missing_intermediate_marker_does_not_turn_grandchild_into_sale() -> None:
    registry = _registry(
        _entry("GRANDCHILD", ("COMB", 1)),
        _entry("CHILD", ("GRANDCHILD", 1)),
        _entry("PARENT", ("CHILD", 1)),
    )
    order = _order(("GRANDCHILD", 1), ("PARENT", 1))
    assert aggregate_orders_demand([order], registry) == {
        "COMB": DemandSample(units=1, order_count=1)
    }


@pytest.mark.parametrize(
    "physical_lines",
    [(), (("SHAMPOO", 1), ("COMB", 1), ("TOWEL", 1))],
)
def test_extra_child_marker_beyond_parent_represents_separate_sale(
    physical_lines: tuple[tuple[str, int], ...],
) -> None:
    order = _order(("PARENT", 1), ("CHILD", 2), *physical_lines)
    assert aggregate_orders_demand([order], NESTED) == {
        "SHAMPOO": DemandSample(units=2, order_count=1),
        "COMB": DemandSample(units=2, order_count=1),
        "TOWEL": DemandSample(units=1, order_count=1),
    }


def test_nested_preexpanded_quantities_and_extra_single_stay_distinct() -> None:
    order = _order(
        ("PARENT", 2),
        ("CHILD", 2),
        ("SHAMPOO", 2),
        ("COMB", 3),  # one standalone comb in addition to two kit combs
        ("TOWEL", 2),
    )
    assert aggregate_orders_demand([order], NESTED) == {
        "SHAMPOO": DemandSample(units=2, order_count=1),
        "COMB": DemandSample(units=3, order_count=1),
        "TOWEL": DemandSample(units=2, order_count=1),
    }


def test_direct_preexpanded_kit_keeps_existing_behavior() -> None:
    registry = _registry(_entry("KIT", ("COMB", 1), ("SHAMPOO", 1)))
    order = _order(("KIT", 1), ("COMB", 1), ("SHAMPOO", 1))
    assert aggregate_orders_demand([order], registry) == {
        "COMB": DemandSample(units=1, order_count=1),
        "SHAMPOO": DemandSample(units=1, order_count=1),
    }


def test_direct_kit_with_missing_component_keeps_whole_definition_fallback() -> None:
    registry = _registry(_entry("KIT", ("COMB", 1), ("SHAMPOO", 1)))
    order = _order(("KIT", 1), ("COMB", 1))
    assert aggregate_orders_demand([order], registry) == {
        "COMB": DemandSample(units=2, order_count=1),
        "SHAMPOO": DemandSample(units=1, order_count=1),
    }


def test_direct_unresolved_kit_keeps_known_component_fallback() -> None:
    registry = _registry(_entry("KIT", ("COMB", 1), (None, 1)))
    assert aggregate_orders_demand([_order(("KIT", 1))], registry) == {
        "COMB": DemandSample(units=1, order_count=1)
    }


def test_unrelated_direct_kits_keep_existing_shared_component_match() -> None:
    registry = _registry(_entry("KIT-A", ("COMB", 1)), _entry("KIT-B", ("COMB", 1)))
    order = _order(("KIT-A", 1), ("KIT-B", 1), ("COMB", 1))
    assert aggregate_orders_demand([order], registry) == {
        "COMB": DemandSample(units=1, order_count=1)
    }


def test_nested_cycle_fails_instead_of_publishing_partial_demand() -> None:
    registry = _registry(_entry("PARENT", ("CHILD", 1)), _entry("CHILD", ("PARENT", 1)))
    with pytest.raises(RuntimeError, match="cycle"):
        aggregate_orders_demand([_order(("PARENT", 1))], registry)


def test_unresolved_nested_child_fails_instead_of_dropping_demand() -> None:
    registry = _registry(_entry("PARENT", ("CHILD", 1)), _entry("CHILD", (None, 1)))
    with pytest.raises(RuntimeError, match="unresolved"):
        aggregate_orders_demand([_order(("PARENT", 1))], registry)


def test_empty_nested_child_fails_instead_of_dropping_demand() -> None:
    registry = _registry(
        _entry("PARENT", ("CHILD", 1), ("TOWEL", 1)),
        _entry("CHILD"),
    )
    with pytest.raises(RuntimeError, match="unresolved"):
        aggregate_orders_demand([_order(("PARENT", 1))], registry)


def test_missing_nested_definition_fails_instead_of_treating_bundle_as_physical() -> None:
    parent = _entry("PARENT", ("CHILD", 1))
    registry = BundleRegistry(
        bundles=(parent,),
        bundle_skus=frozenset({"PARENT", "CHILD"}),
        by_bundle_sku={"PARENT": parent},
    )
    with pytest.raises(RuntimeError, match="unresolved nested bundle CHILD"):
        aggregate_orders_demand([_order(("PARENT", 1))], registry)


def test_stock_flagged_child_without_recipe_fails_with_production_shaped_registry() -> None:
    registry = _registry(_entry("PARENT", ("CHILD", 1), ("TOWEL", 1)))
    with pytest.raises(RuntimeError, match="unresolved nested bundle CHILD"):
        aggregate_orders_demand(
            [_order(("PARENT", 1))], registry, known_kit_skus={"PARENT", "CHILD"}
        )


def test_stock_flagged_kit_without_recipe_cannot_be_counted_as_physical() -> None:
    with pytest.raises(RuntimeError, match="unresolved bundle UNKNOWN"):
        aggregate_orders_demand([_order(("UNKNOWN", 1))], _registry(), known_kit_skus={"UNKNOWN"})


@pytest.mark.parametrize(
    "entry",
    [_entry("KIT"), _entry("KIT", ("COMB", 1), (None, 1))],
)
def test_stock_flagged_direct_kit_with_incomplete_recipe_fails(entry: BundleEntry) -> None:
    with pytest.raises(RuntimeError, match="unresolved bundle components in KIT"):
        aggregate_orders_demand([_order(("KIT", 1))], _registry(entry), known_kit_skus={"KIT"})


def test_complete_nested_registry_accepts_stock_kit_flags() -> None:
    order = _order(("PARENT", 1), ("CHILD", 1), ("SHAMPOO", 1), ("COMB", 1), ("TOWEL", 1))
    assert aggregate_orders_demand([order], NESTED, known_kit_skus={"PARENT", "CHILD"}) == {
        "SHAMPOO": DemandSample(units=1, order_count=1),
        "COMB": DemandSample(units=1, order_count=1),
        "TOWEL": DemandSample(units=1, order_count=1),
    }
