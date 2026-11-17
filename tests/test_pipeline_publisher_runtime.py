from unittest.mock import Mock

import pytest

from based_inventory.pipeline.google_api import DYNAMIC_ID, ERP_ID, GoogleAPIError
from based_inventory.pipeline.merchant_publish import build_merchant_plan
from based_inventory.pipeline.publisher import MerchantPublisher
from based_inventory.pipeline.store import Store
from tests.test_merchant_publish import inputs


def test_lost_write_response_is_read_back_and_not_appended_twice():
    version, history, rolling, daily = inputs()
    planned = build_merchant_plan(
        version, history, rolling, daily, [["sku", "Product", 1]], "source", {}
    )
    api = Mock()
    store = Mock(spec=Store)
    store.plan_once.return_value = planned | {
        "history_before": history,
        "rolling_before": rolling,
        "daily_before": daily,
    }
    publisher = MerchantPublisher(store, api, sleep=Mock())
    publisher.collect = Mock(
        side_effect=[
            {
                "history": history,
                "rolling": rolling,
                "daily": daily,
                "map": [["sku", "Product", 1]],
                "notes": {},
            },
            {
                "history": planned["history_after"],
                "rolling": planned["rolling_after"],
                "daily": planned["daily_after"],
            },
        ]
    )
    publisher.verify_canonical = Mock()
    publisher.verify_inventory = Mock()
    api.batch.side_effect = [GoogleAPIError("response lost"), None]
    publisher.publish(version, "source")
    assert api.batch.call_args_list[0].args[0] == DYNAMIC_ID
    assert api.batch.call_args_list[1].args[0] == ERP_ID
    assert api.batch.call_count == 2
    assert {c.args[1] for c in store.record_receipt.call_args_list} == {
        "history",
        "rolling",
        "erp",
        "canonical",
        "inventory",
    }


def test_external_history_edit_blocks_stale_plan_before_any_write():
    version, history, rolling, daily = inputs()
    plan = build_merchant_plan(
        version, history, rolling, daily, [["sku", "Product", 1]], "source", {}
    )
    store = Mock(spec=Store)
    store.plan_once.return_value = plan | {
        "history_before": history,
        "rolling_before": rolling,
        "daily_before": daily,
    }
    api = Mock()
    publisher = MerchantPublisher(store, api)
    edited = [list(r) for r in history]
    edited[1][2] = 999
    publisher.collect = Mock(
        return_value={
            "history": edited,
            "rolling": rolling,
            "daily": daily,
            "map": [["sku", "Product", 1]],
            "notes": {},
        }
    )
    with pytest.raises(ValueError, match="changed outside"):
        publisher.publish(version, "source")
    api.batch.assert_not_called()
