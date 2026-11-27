"""Tests for Slack Block Kit poster."""

from unittest.mock import MagicMock

import pytest
import requests

from based_inventory.slack import SlackClient, SlackPostUncertainError


def test_post_message_success(monkeypatch):
    mock_post = MagicMock(return_value=_mock_response({"ok": True, "ts": "123.456"}))
    monkeypatch.setattr("based_inventory.slack.requests.post", mock_post)

    client = SlackClient(token="xoxb-test", channel="C123")
    ok = client.post_message(fallback_text="hi", blocks=[{"type": "section"}])

    assert ok is True
    args, kwargs = mock_post.call_args
    assert args[0] == "https://slack.com/api/chat.postMessage"
    body = kwargs["json"]
    assert body["channel"] == "C123"
    assert body["text"] == "hi"
    assert body["blocks"] == [{"type": "section"}]
    assert body["unfurl_links"] is False


def test_post_message_returns_false_on_not_ok(monkeypatch):
    mock_post = MagicMock(return_value=_mock_response({"ok": False, "error": "channel_not_found"}))
    monkeypatch.setattr("based_inventory.slack.requests.post", mock_post)

    client = SlackClient(token="xoxb-test", channel="C123")
    ok = client.post_message(fallback_text="hi", blocks=[])

    assert ok is False


def test_dry_run_does_not_call_api(monkeypatch, capsys):
    mock_post = MagicMock()
    monkeypatch.setattr("based_inventory.slack.requests.post", mock_post)

    client = SlackClient(token="xoxb-test", channel="C123", dry_run=True)
    ok = client.post_message(fallback_text="hi", blocks=[{"type": "section"}])

    assert ok is True
    mock_post.assert_not_called()
    captured = capsys.readouterr()
    assert "[DRY_RUN]" in captured.out
    assert "hi" in captured.out


def test_post_with_receipt_threads_without_broadcast(monkeypatch):
    mock_post = MagicMock(
        return_value=_mock_response({"ok": True, "channel": "C123", "ts": "123.456789"})
    )
    monkeypatch.setattr("based_inventory.slack.requests.post", mock_post)
    client = SlackClient("token", "C123")

    assert client.post_with_receipt("hourly", [], thread_ts="100.000001") == "123.456789"
    body = mock_post.call_args.kwargs["json"]
    assert body["thread_ts"] == "100.000001"
    assert body["reply_broadcast"] is False


@pytest.mark.parametrize("error", ["thread_not_found", "channel_not_found"])
def test_post_with_receipt_drops_definite_rejections(monkeypatch, error):
    mock_post = MagicMock(return_value=_mock_response({"ok": False, "error": error}))
    monkeypatch.setattr("based_inventory.slack.requests.post", mock_post)

    assert SlackClient("token", "C123").post_with_receipt("hourly", [], thread_ts=None) is None
    assert "thread_ts" not in mock_post.call_args.kwargs["json"]


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "channel": "C124", "ts": "123.456789"},
        {"ok": True, "channel": "C123"},
        {"ok": False, "error": "internal_error"},
    ],
)
def test_post_with_receipt_marks_ambiguous_results(monkeypatch, payload):
    monkeypatch.setattr(
        "based_inventory.slack.requests.post", MagicMock(return_value=_mock_response(payload))
    )
    with pytest.raises(SlackPostUncertainError):
        SlackClient("token", "C123").post_with_receipt("hourly", [], thread_ts=None)


def test_post_with_receipt_timeout_is_uncertain(monkeypatch):
    monkeypatch.setattr(
        "based_inventory.slack.requests.post", MagicMock(side_effect=requests.Timeout)
    )
    with pytest.raises(SlackPostUncertainError):
        SlackClient("token", "C123").post_with_receipt("hourly", [], thread_ts=None)


def _mock_response(payload):
    response = MagicMock()
    response.status_code = 200
    response.ok = True
    response.json.return_value = payload
    return response
