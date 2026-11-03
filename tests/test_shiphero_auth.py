"""Token refresh must ride out temporary provider errors without leaking tokens."""

from __future__ import annotations

import json
import time
import traceback
from unittest.mock import Mock

import pytest
import requests

from based_inventory import shiphero_auth


def _response(status: int, body: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()
    return response


def _success() -> requests.Response:
    return _response(200, json.dumps({"access_token": "fresh-access-token", "expires_in": 2419200}))


@pytest.fixture
def refresh_io(monkeypatch):
    post = Mock()
    waits = []
    monkeypatch.setattr(shiphero_auth.requests, "post", post)
    monkeypatch.setattr(time, "sleep", waits.append)
    return post, waits


def test_refresh_returns_fresh_token_and_preserves_request_contract(refresh_io) -> None:
    post, waits = refresh_io
    post.return_value = _success()

    assert shiphero_auth.refresh_access_token("refresh-secret", timeout=9) == "fresh-access-token"
    assert post.call_args.args == ("https://public-api.shiphero.com/auth/refresh",)
    assert post.call_args.kwargs == {
        "json": {"refresh_token": "refresh-secret"},
        "headers": {"Content-Type": "application/json"},
        "timeout": 9,
    }
    assert waits == []


@pytest.mark.parametrize("body", ["<html><h1>403 Forbidden</h1></html>", "Forbidden"])
def test_refresh_recovers_from_non_json_403(refresh_io, body) -> None:
    post, waits = refresh_io
    post.side_effect = [_response(403, body), _success()]

    assert shiphero_auth.refresh_access_token("refresh-secret") == "fresh-access-token"
    assert post.call_count == 2
    assert waits == [5]


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_refresh_recovers_from_temporary_http_failure(refresh_io, status) -> None:
    post, waits = refresh_io
    post.side_effect = [_response(status, "provider unavailable"), _success()]

    assert shiphero_auth.refresh_access_token("refresh-secret") == "fresh-access-token"
    assert post.call_count == 2
    assert waits == [5]


@pytest.mark.parametrize("status", [400, 401, 403])
def test_refresh_rejects_json_auth_errors_without_retry_or_body_leak(refresh_io, status) -> None:
    post, waits = refresh_io
    post.return_value = _response(status, '{"error": "refresh-secret is invalid"}')

    with pytest.raises(RuntimeError, match=f"HTTP {status}") as caught:
        shiphero_auth.refresh_access_token("refresh-secret")

    assert "refresh-secret" not in str(caught.value)
    assert post.call_count == 1
    assert waits == []


def test_refresh_persistent_non_json_403_is_bounded_and_sanitized(refresh_io, caplog) -> None:
    post, waits = refresh_io
    post.return_value = _response(403, "<html>refresh-secret and provider internals</html>")

    with pytest.raises(RuntimeError, match="HTTP 403.*after 4 attempts") as caught:
        shiphero_auth.refresh_access_token("refresh-secret")

    assert post.call_count == 4
    assert waits == [5, 15, 30]
    rendered = "".join(traceback.format_exception(type(caught.value), caught.value, caught.tb))
    assert "refresh-secret" not in str(caught.value)
    assert "provider internals" not in rendered
    assert "refresh-secret" not in caplog.text


@pytest.mark.parametrize("error", [requests.ConnectionError, requests.Timeout])
def test_refresh_recovers_from_network_failure(refresh_io, error) -> None:
    post, waits = refresh_io
    post.side_effect = [error("refresh-secret in transport details"), _success()]

    assert shiphero_auth.refresh_access_token("refresh-secret") == "fresh-access-token"
    assert waits == [5]


def test_refresh_persistent_network_failure_hides_exception_details(refresh_io, caplog) -> None:
    post, waits = refresh_io
    post.side_effect = requests.Timeout("refresh-secret in transport details")

    with pytest.raises(RuntimeError, match="network failure.*after 4 attempts") as caught:
        shiphero_auth.refresh_access_token("refresh-secret")

    assert post.call_count == 4
    assert waits == [5, 15, 30]
    rendered = "".join(traceback.format_exception(type(caught.value), caught.value, caught.tb))
    assert "transport details" not in rendered
    assert "refresh-secret" not in caplog.text


def test_refresh_recovers_from_non_json_success_response(refresh_io) -> None:
    post, waits = refresh_io
    post.side_effect = [_response(200, "<html>upstream error</html>"), _success()]

    assert shiphero_auth.refresh_access_token("refresh-secret") == "fresh-access-token"
    assert waits == [5]


@pytest.mark.parametrize(
    "payload", [{"error": "refresh-secret"}, [], {"access_token": 123}, {"access_token": " "}]
)
def test_refresh_rejects_invalid_success_payload_without_body_leak(refresh_io, payload) -> None:
    post, waits = refresh_io
    post.return_value = _response(200, json.dumps(payload))

    with pytest.raises(RuntimeError, match="missing or invalid access_token") as caught:
        shiphero_auth.refresh_access_token("refresh-secret")

    assert "refresh-secret" not in str(caught.value)
    assert post.call_count == 1
    assert waits == []


def test_resolve_does_not_fall_back_after_refresh_failure(refresh_io) -> None:
    post, _waits = refresh_io
    post.return_value = _response(401, '{"error": "invalid grant"}')

    with pytest.raises(RuntimeError, match="HTTP 401"):
        shiphero_auth.resolve_access_token("refresh-secret", "stale-fallback")


def test_resolve_uses_fallback_only_without_refresh_token() -> None:
    assert shiphero_auth.resolve_access_token(None, "fallback-token") == "fallback-token"


def test_resolve_rejects_missing_credentials() -> None:
    with pytest.raises(RuntimeError, match="cannot authenticate"):
        shiphero_auth.resolve_access_token(None, None)
