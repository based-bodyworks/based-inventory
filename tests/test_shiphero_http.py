"""ShipHero failures must not look like valid inventory observations."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from based_inventory.jobs.quantity_alerts import _fetch_candidate_depletion
from based_inventory.shiphero import ShipHeroClient


def _response(status: int, body: str, content_type: str | None = "application/json"):
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()
    if content_type is not None:
        response.headers["Content-Type"] = content_type
    return response


def _scripted_post(monkeypatch, responses):
    calls = []
    answers = iter(responses)

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return next(answers)

    monkeypatch.setattr("based_inventory.shiphero.requests.post", post)
    return calls


def test_retries_non_json_503_then_accepts_valid_json(monkeypatch) -> None:
    calls = _scripted_post(
        monkeypatch,
        [
            _response(503, "<html>temporary outage</html>", "text/html"),
            _response(200, '{"data":{"ok":true}}'),
        ],
    )
    waits = []
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", waits.append)

    result = ShipHeroClient(token="secret")._execute("query", retries=1)

    assert result == {"data": {"ok": True}}
    assert len(calls) == 2
    assert waits == [2]


@pytest.mark.parametrize(
    ("responses", "expected_type"),
    [
        (
            [_response(200, "<html>private response</html>", "text/html") for _ in range(2)],
            "non-json",
        ),
        ([_response(200, "not JSON") for _ in range(2)], "json"),
        ([_response(200, "[]") for _ in range(2)], "json"),
        ([_response(200, "") for _ in range(2)], "json"),
    ],
)
def test_bad_success_response_fails_closed_without_body_or_token(
    monkeypatch, responses, expected_type
) -> None:
    calls = _scripted_post(monkeypatch, responses)
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", lambda _: None)

    with pytest.raises(
        RuntimeError, match=f"ShipHero HTTP 200 invalid {expected_type} response"
    ) as caught:
        ShipHeroClient(token="secret")._execute("query", retries=1)

    assert len(calls) == 2
    assert "private response" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_non_retryable_http_error_does_not_parse_or_reveal_body(monkeypatch) -> None:
    calls = _scripted_post(monkeypatch, [_response(401, "token secret denied", "text/html")])

    with pytest.raises(RuntimeError, match="ShipHero HTTP 401 \\(non-json response\\)") as caught:
        ShipHeroClient(token="secret")._execute("query")

    assert len(calls) == 1
    assert "token secret denied" not in str(caught.value)


def test_persistent_503_fails_with_status_and_type_only(monkeypatch) -> None:
    calls = _scripted_post(
        monkeypatch,
        [_response(503, "<html>token secret</html>", "text/html") for _ in range(2)],
    )
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="ShipHero HTTP 503 \\(non-json response\\)") as caught:
        ShipHeroClient(token="secret")._execute("query", retries=1)

    assert len(calls) == 2
    assert "token secret" not in str(caught.value)


def test_json_without_content_type_remains_compatible(monkeypatch) -> None:
    _scripted_post(monkeypatch, [_response(200, '{"data":{"ok":true}}', None)])

    assert ShipHeroClient(token="secret")._execute("query", retries=0) == {"data": {"ok": True}}


def test_429_throttle_hint_remains_bounded(monkeypatch) -> None:
    throttle = json.dumps({"errors": [{"code": 30, "time_remaining": "2"}]})
    _scripted_post(monkeypatch, [_response(429, throttle), _response(200, '{"data":{}}')])
    waits = []
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", waits.append)

    assert ShipHeroClient(token="secret")._execute("query", retries=1) == {"data": {}}
    assert waits == [15]


def test_repeated_credit_contention_can_recover_after_six_shortages(monkeypatch) -> None:
    shortage = json.dumps(
        {
            "errors": [
                {
                    "code": 30,
                    "required_credits": 401,
                    "remaining_credits": 43,
                    "time_remaining": "5 seconds",
                }
            ],
            "extensions": {"throttling": {"user_quota": {"max_available": 4004}}},
        }
    )
    responses = [_response(200, shortage) for _ in range(7)]
    responses.append(_response(200, '{"data":{"ok":true}}'))
    calls = _scripted_post(monkeypatch, responses)
    waits = []
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", waits.append)

    assert ShipHeroClient(token="secret")._execute("query")["data"] == {"ok": True}
    assert len(calls) == 8
    assert waits == [15] * 7


def test_credit_wait_uses_deficit_when_provider_hint_is_too_short(monkeypatch) -> None:
    shortage = json.dumps(
        {
            "errors": [
                {
                    "code": 30,
                    "required_credits": 1601,
                    "remaining_credits": 100,
                    "time_remaining": "1 second",
                }
            ],
            "extensions": {
                "throttling": {"user_quota": {"max_available": 4004, "increment_rate": 60}}
            },
        }
    )
    _scripted_post(monkeypatch, [_response(200, shortage), _response(200, '{"data":{}}')])
    waits = []
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", waits.append)

    assert ShipHeroClient(token="secret")._execute("query", retries=1) == {"data": {}}
    assert waits == [27]


def test_credit_refill_waits_and_retries_only_the_current_request(monkeypatch, caplog) -> None:
    shortage = {
        "errors": [
            {
                "message": "There are not enough credits: private-provider-detail",
                "code": 30,
                "required_credits": 1601,
                "remaining_credits": 594,
                "time_remaining": "17 seconds",
            }
        ],
        "extensions": {"throttling": {"user_quota": {"max_available": 4004}}},
    }
    calls = _scripted_post(
        monkeypatch,
        [_response(200, json.dumps(shortage)), _response(200, '{"data":{"ok":true}}')],
    )
    waits = []
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", waits.append)

    assert ShipHeroClient(token="secret")._execute("query", retries=1)["data"] == {"ok": True}
    assert len(calls) == 2
    assert waits == [18]
    assert "required=1601" in caplog.text
    assert "remaining=594" in caplog.text
    assert "private-provider-detail" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("include_quota", [True, False])
def test_credit_request_above_account_cap_fails_without_wait(monkeypatch, include_quota) -> None:
    oversized = {
        "errors": [
            {
                "message": "There are not enough credits: private-provider-detail",
                "code": 30,
                "required_credits": 5000,
                "remaining_credits": 594,
                "time_remaining": "17 seconds",
            }
        ],
    }
    if include_quota:
        oversized["extensions"] = {"throttling": {"user_quota": {"max_available": 4004}}}
    else:
        oversized["errors"][0]["message"] = "operation requires too many credits"
    calls = _scripted_post(monkeypatch, [_response(200, json.dumps(oversized))])
    waits = []
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", waits.append)

    with pytest.raises(RuntimeError, match="^ShipHero GraphQL credits limit$"):
        ShipHeroClient(token="secret")._execute("query")
    assert len(calls) == 1
    assert waits == []


def test_credit_error_without_live_cap_retries_when_provider_does_not_say_oversized(
    monkeypatch,
) -> None:
    shortage = {
        "errors": [
            {
                "message": "There are not enough credits",
                "code": 30,
                "required_credits": 5000,
                "remaining_credits": 100,
                "time_remaining": "17 seconds",
            }
        ]
    }
    calls = _scripted_post(
        monkeypatch,
        [_response(200, json.dumps(shortage)), _response(200, '{"data":{"ok":true}}')],
    )
    waits = []
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", waits.append)

    assert ShipHeroClient(token="secret")._execute("query", retries=1)["data"] == {"ok": True}
    assert len(calls) == 2
    assert waits == [18]


def test_429_credit_error_at_retry_limit_keeps_credit_signal(monkeypatch) -> None:
    body = json.dumps(
        {"errors": [{"code": 30, "message": "not enough credits", "time_remaining": "2 seconds"}]}
    )
    _scripted_post(monkeypatch, [_response(429, body)])

    with pytest.raises(RuntimeError, match="^ShipHero GraphQL not enough credits$"):
        ShipHeroClient(token="secret")._execute("query", retries=0)


def test_graphql_error_does_not_reveal_provider_message(monkeypatch) -> None:
    body = json.dumps({"errors": [{"message": "private query and token secret"}]})
    _scripted_post(monkeypatch, [_response(200, body)])

    with pytest.raises(RuntimeError, match="ShipHero GraphQL error") as caught:
        ShipHeroClient(token="secret")._execute("query", retries=0)
    assert "private query" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_credit_cap_error_keeps_a_fixed_bisection_signal(monkeypatch) -> None:
    body = json.dumps({"errors": [{"message": "Exceeded available credits: private details"}]})
    _scripted_post(monkeypatch, [_response(200, body)])

    with pytest.raises(RuntimeError, match="ShipHero GraphQL credits limit") as caught:
        ShipHeroClient(token="secret")._execute("query", retries=0)
    assert "private details" not in str(caught.value)


def test_channel_mix_bisects_on_sanitized_credit_error(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    calls = []

    def execute(query, variables=None, retries=6):
        calls.append(variables)
        if len(calls) == 1:
            raise RuntimeError("ShipHero GraphQL credits limit")
        order = {"order_number": str(len(calls)), "shop_name": "BASED"}
        return {
            "data": {
                "orders": {"data": {"edges": [{"node": order}], "pageInfo": {"hasNextPage": False}}}
            }
        }

    monkeypatch.setattr(client, "_execute", execute)
    assert client.fetch_channel_mix("2026-10-01T00:00:00") == {"BASED": 2}
    assert len(calls) == 3
    assert calls[1]["until"] == calls[2]["since"]


def test_inbound_bisects_on_sanitized_credit_error(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    calls = []

    def execute(query, variables=None, retries=6):
        calls.append(variables)
        if len(calls) == 1:
            raise RuntimeError("ShipHero GraphQL credits limit")
        po = {
            "po_number": str(len(calls)),
            "po_date": "2026-10-01T00:00:00",
            "ship_date": None,
            "line_items": {
                "edges": [{"node": {"sku": "SKU1", "quantity": 10, "quantity_received": 2}}]
            },
        }
        return {
            "data": {
                "purchase_orders": {
                    "data": {"edges": [{"node": po}], "pageInfo": {"hasNextPage": False}}
                }
            }
        }

    monkeypatch.setattr(client, "_execute", execute)
    result = client.fetch_inbound_outstanding_by_sku("2026-10-01T00:00:00")
    assert len(calls) == 3
    assert calls[1]["until"] == calls[2]["since"]
    assert result["SKU1"]["outstanding"] == 16
    assert result["SKU1"]["po_count"] == 2


def test_network_error_is_sanitized(monkeypatch) -> None:
    def failed_post(*args, **kwargs):
        raise requests.ConnectionError("private URL and token secret")

    monkeypatch.setattr("based_inventory.shiphero.requests.post", failed_post)
    with pytest.raises(RuntimeError, match="ShipHero network error after 0 retries") as caught:
        ShipHeroClient(token="secret")._execute("query", retries=0)
    assert "private URL" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_quantity_depletion_source_failure_stops_alert_calculation() -> None:
    class BrokenClient:
        def fetch_sku_depletion(self, **kwargs):
            raise RuntimeError("ShipHero HTTP 503 (non-json response)")

    with pytest.raises(RuntimeError, match="ShipHero HTTP 503"):
        _fetch_candidate_depletion(BrokenClient(), [SimpleNamespace(sku="SKU1")], "2026-10-16")


def test_quantity_depletion_observed_zero_remains_zero() -> None:
    class ZeroClient:
        def fetch_sku_depletion(self, **kwargs):
            return 0, 7.0

    assert _fetch_candidate_depletion(
        ZeroClient(), [SimpleNamespace(sku="SKU1")], "2026-10-16"
    ) == ({"SKU1": 0}, {"SKU1": 7.0})


def _line_page(items=(), *, more=False, cursor=None):
    def line(item):
        if len(item) == 2:
            sku, quantity = item
            line_id = sku
        else:
            line_id, sku, quantity = item
        return {"node": {"id": line_id, "sku": sku, "quantity": quantity}}

    return {
        "edges": [line(item) for item in items],
        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
    }


def _order_node(order_id, *, date="2026-11-11T00:40:00", number=None, lines=None):
    return {
        "id": order_id,
        "order_number": number or order_id,
        "order_date": date,
        "shop_name": "BASED",
        "line_items": lines if lines is not None else _line_page(),
    }


def _orders_page(nodes, *, more=False, cursor=None, count=None):
    return {
        "data": {
            "orders": {
                "data": {
                    "edges": [{"node": node} for node in nodes],
                    "total_count": len(nodes) if count is None else count,
                    "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                }
            }
        }
    }


def _order_lines_page(order_id, lines):
    return {"data": {"order": {"data": {"id": order_id, "line_items": lines}}}}


def _script_client_pages(monkeypatch, client, pages, *, final_count=None):
    calls = []
    answers = iter(pages)
    initial_count = pages[0]["data"]["orders"]["data"]["total_count"]
    count_calls = 0

    def execute(query, variables=None, retries=6):
        nonlocal count_calls
        calls.append((query, variables))
        if "total_count" in query:
            count_calls += 1
            count = initial_count if count_calls == 1 or final_count is None else final_count
            return {"data": {"orders": {"data": {"total_count": count}}}}
        return next(answers)

    monkeypatch.setattr(client, "_execute", execute)
    return calls


def test_fetch_orders_for_day_uses_complete_cursor_fetch(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    arguments = []

    def fetch_orders_window(*args, **kwargs):
        arguments.append((args, kwargs))
        return [_order_node("A")]

    monkeypatch.setattr(client, "fetch_orders_window", fetch_orders_window)

    assert [order["id"] for order in client.fetch_orders_for_day("2026-11-11", max_pages=20)] == [
        "A"
    ]
    assert arguments == [
        (
            ("2026-11-11T00:00:00",),
            {
                "until_iso": "2026-11-12T00:00:00",
                "warehouse_id": "V2FyZWhvdXNlOjExNzY2MQ==",
                "max_pages": 20,
            },
        )
    ]


def test_order_cursor_paging_keeps_orders_with_earlier_and_equal_order_dates(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    calls = _script_client_pages(
        monkeypatch,
        client,
        [
            _orders_page(
                [_order_node("A", lines=_line_page([("COMB", 1)]))],
                more=True,
                cursor="a",
                count=3,
            ),
            _orders_page(
                [
                    _order_node("B", date="2026-11-11T00:00:00", lines=_line_page([("COMB", 2)])),
                    _order_node("C", lines=_line_page([("COMB", 3)])),
                ],
                count=3,
            ),
        ],
    )

    result = client.fetch_orders_window("2026-11-11T00:00:00", until_iso="2026-11-12T00:00:00")

    assert [order["id"] for order in result] == ["A", "B", "C"]
    page_calls = [call for call in calls if "data(first: 25" in call[0]]
    assert [call[1]["after"] for call in page_calls] == [None, "a"]
    assert [call[1]["since"] for call in page_calls] == ["2026-11-11T00:00:00"] * 2
    assert [call[1]["until"] for call in page_calls] == ["2026-11-12T00:00:00"] * 2
    assert "data(first: 25, after: $after)" in page_calls[0][0]


@pytest.mark.parametrize(
    ("pages", "limit", "error"),
    [
        ([_orders_page([_order_node("A")], more=True)], 2, "cursor missing"),
        (
            [
                _orders_page([_order_node("A")], more=True, cursor="same"),
                _orders_page([_order_node("B")], more=True, cursor="same"),
            ],
            3,
            "cursor did not advance",
        ),
        ([_orders_page([_order_node("A")], more=True, cursor="next")], 1, "exceeded 1 pages"),
        ([_orders_page([], more=True, cursor="next")], 1, "empty page"),
        (
            [
                _orders_page([_order_node("A")], more=True, cursor="next"),
                _orders_page([_order_node("A")]),
            ],
            2,
            "repeated order id",
        ),
        ([_orders_page([{"order_number": "A"}])], 1, "order id missing"),
    ],
)
def test_order_pagination_failures_never_return_partial_day(
    monkeypatch, pages, limit, error
) -> None:
    client = ShipHeroClient(token="secret")
    _script_client_pages(monkeypatch, client, pages)

    with pytest.raises(RuntimeError, match=error):
        client.fetch_orders_window("2026-11-11T00:00:00", max_pages=limit)


def test_orders_with_same_store_number_but_distinct_ids_are_not_dropped(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    _script_client_pages(
        monkeypatch,
        client,
        [_orders_page([_order_node("tiktok", number="123"), _order_node("shopify", number="123")])],
    )

    assert [order["id"] for order in client.fetch_orders_window("2026-11-11T00:00:00")] == [
        "tiktok",
        "shopify",
    ]


def test_orders_total_count_mismatch_never_returns_partial_day(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    calls = _script_client_pages(monkeypatch, client, [_orders_page([_order_node("A")], count=2)])

    with pytest.raises(RuntimeError, match="orders total_count mismatch"):
        client.fetch_orders_window("2026-11-11T00:00:00")
    assert "total_count" in calls[0][0]


def test_orders_total_count_change_never_returns_partial_day(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    _script_client_pages(monkeypatch, client, [_orders_page([_order_node("A")])], final_count=2)

    with pytest.raises(RuntimeError, match="orders total_count changed"):
        client.fetch_orders_window("2026-11-11T00:00:00")


def test_order_count_is_checked_before_and_after_without_recounting_each_page(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    count_calls = 0
    page_calls = 0

    def execute(query, variables=None, retries=6):
        nonlocal count_calls, page_calls
        if "total_count" in query:
            count_calls += 1
            return {"data": {"orders": {"data": {"total_count": 2}}}}
        page_calls += 1
        if page_calls == 1:
            return _orders_page([_order_node("A")], more=True, cursor="next")
        return _orders_page([_order_node("B")])

    monkeypatch.setattr(client, "_execute", execute)
    assert [order["id"] for order in client.fetch_orders_window("2026-11-11T00:00:00")] == [
        "A",
        "B",
    ]
    assert count_calls == 2
    assert page_calls == 2


def test_credit_refill_retries_same_order_cursor_page(monkeypatch) -> None:
    count = {"data": {"orders": {"data": {"total_count": 2}}}}
    first = _orders_page([_order_node("A")], more=True, cursor="cursor-a")
    shortage = {
        "errors": [
            {
                "code": 30,
                "message": "There are not enough credits",
                "required_credits": 401,
                "remaining_credits": 100,
                "time_remaining": "1 second",
            }
        ]
    }
    second = _orders_page([_order_node("B")])
    calls = _scripted_post(
        monkeypatch,
        [_response(200, json.dumps(value)) for value in (count, first, shortage, second, count)],
    )
    waits = []
    monkeypatch.setattr("based_inventory.shiphero.time.sleep", waits.append)

    result = ShipHeroClient(token="secret").fetch_orders_window("2026-11-11T00:00:00")

    assert [order["id"] for order in result] == ["A", "B"]
    variables = [call[1]["json"]["variables"] for call in calls]
    assert [value.get("after") for value in variables] == [None, None, "cursor-a", "cursor-a", None]
    assert waits == [0.2, 15]


def test_long_order_fetches_remaining_line_items_by_order_cursor(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    calls = _script_client_pages(
        monkeypatch,
        client,
        [
            _orders_page(
                [_order_node("A", lines=_line_page([("COMB", 1)], more=True, cursor="line-1"))]
            ),
            _order_lines_page("A", _line_page([("GEL", 2)], cursor="line-2")),
        ],
    )

    result = client.fetch_orders_window("2026-11-11T00:00:00")

    assert [edge["node"]["sku"] for edge in result[0]["line_items"]["edges"]] == ["COMB", "GEL"]
    line_calls = [call for call in calls if "order(id:" in call[0]]
    assert len(line_calls) == 1
    assert line_calls[0][1] == {"order_id": "A", "line_after": "line-1"}
    assert "line_items(first: 15, after: $line_after)" in line_calls[0][0]


def test_long_order_keeps_distinct_lines_with_same_sku(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    _script_client_pages(
        monkeypatch,
        client,
        [
            _orders_page(
                [_order_node("A", lines=_line_page([("line-1", "COMB", 1)], more=True, cursor="a"))]
            ),
            _order_lines_page("A", _line_page([("line-2", "COMB", 2)])),
        ],
    )

    result = client.fetch_orders_window("2026-11-11T00:00:00")
    assert [edge["node"]["quantity"] for edge in result[0]["line_items"]["edges"]] == [1, 2]


def test_long_order_rejects_overlapping_line_id_across_cursor_pages(monkeypatch) -> None:
    client = ShipHeroClient(token="secret")
    _script_client_pages(
        monkeypatch,
        client,
        [
            _orders_page(
                [_order_node("A", lines=_line_page([("line-1", "COMB", 1)], more=True, cursor="a"))]
            ),
            _order_lines_page("A", _line_page([("line-1", "COMB", 1)], cursor="b")),
        ],
    )

    with pytest.raises(RuntimeError, match="repeated line id.*order A"):
        client.fetch_orders_window("2026-11-11T00:00:00")


@pytest.mark.parametrize(
    ("first_cursor", "followup_id", "followup_lines", "line_page_limit", "error"),
    [
        (None, "A", _line_page([("GEL", 1)]), 20, "cursor missing"),
        (
            "line-1",
            "A",
            _line_page([("GEL", 1)], more=True, cursor="line-1"),
            20,
            "cursor did not advance",
        ),
        (
            "line-1",
            "A",
            _line_page([("GEL", 1)], more=True, cursor="line-2"),
            2,
            "exceeded 2 pages",
        ),
        ("line-1", "B", _line_page([("GEL", 1)]), 20, "order id changed"),
        ("line-1", "A", _line_page(), 20, "empty page"),
    ],
)
def test_long_order_pagination_failures_never_return_partial_lines(
    monkeypatch, first_cursor, followup_id, followup_lines, line_page_limit, error
) -> None:
    client = ShipHeroClient(token="secret")
    monkeypatch.setattr(client, "_ORDERS_LINE_ITEMS_MAX_PAGES", line_page_limit)
    _script_client_pages(
        monkeypatch,
        client,
        [
            _orders_page(
                [_order_node("A", lines=_line_page([("COMB", 1)], more=True, cursor=first_cursor))]
            ),
            _order_lines_page(followup_id, followup_lines),
        ],
    )

    with pytest.raises(RuntimeError, match=error):
        client.fetch_orders_window("2026-11-11T00:00:00")
