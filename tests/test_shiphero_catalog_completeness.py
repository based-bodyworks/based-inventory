"""Native sales must not use a silently truncated kit or product catalog."""

import pytest

from based_inventory.shiphero import ShipHeroClient


def client_with_page(monkeypatch, kind, has_next):
    node = (
        {"sku": "KIT", "name": "Kit", "kit_components": [{"sku": "SALT", "quantity": 2}]}
        if kind == "kits"
        else {"sku": "SALT", "on_hand": 5, "available": 3, "product": {"name": "Salt"}}
    )
    key = "products" if kind == "kits" else "warehouse_products"
    client = ShipHeroClient(token="test-token")
    monkeypatch.setattr(
        client,
        "_execute",
        lambda *a, **k: {
            "data": {
                key: {"data": {"edges": [{"node": node}], "pageInfo": {"hasNextPage": has_next}}}
            }
        },
    )
    return client.fetch_all_kits if kind == "kits" else client.fetch_warehouse_stock


@pytest.mark.parametrize("kind", ["kits", "stock"])
def test_strict_catalog_refuses_remaining_pages_even_when_page_is_short(monkeypatch, kind):
    fetch = client_with_page(monkeypatch, kind, True)
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="complete-read budget"):
        fetch(require_complete=True, max_calls=1)


@pytest.mark.parametrize("kind", ["kits", "stock"])
def test_strict_catalog_refuses_a_remaining_page_in_unsplittable_window(monkeypatch, kind):
    fetch = client_with_page(monkeypatch, kind, True)
    with pytest.raises(RuntimeError, match="incomplete date window"):
        fetch(
            require_complete=True,
            updated_from="2026-11-18T10:00:00",
            updated_to="2026-11-18T10:00:00",
        )


@pytest.mark.parametrize("kind", ["kits", "stock"])
def test_strict_catalog_accepts_a_proven_final_page(monkeypatch, kind):
    fetch = client_with_page(monkeypatch, kind, False)
    assert len(fetch(require_complete=True, max_calls=1)) == 1
