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
    assert waits == [3]


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
