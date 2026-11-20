from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests

from based_inventory.pipeline.models import digest
from based_inventory.pipeline.slack_report import SlackReport, SlackReportError
from based_inventory.pipeline.store import Store
from tests.test_pipeline_models import verified


def publication_client():
    client = SlackReport("token", "C123", session=Mock())
    client.call = Mock(
        side_effect=[
            {"file_id": "F123", "upload_url": "https://files.slack.com/upload"},
            {"ok": True},
        ]
    )
    client.session.post.return_value.status_code = 200
    client.verify = Mock()
    return client


def test_publication_uses_exact_full_report_text():
    client = publication_client()
    text = "*Daily SKU velocity*\nEvery tracked SKU is visible."
    client.publish(store_with(), verified(), summary_text=text)
    assert client.call.call_args_list[1].args[1]["initial_comment"] == text


def test_cloud_publication_shows_all_catalog_skus_and_complete_seven_day_pace():
    version = verified()
    history = {
        version.day - timedelta(days=n): verified(day=version.day - timedelta(days=n))
        for n in range(7)
    }
    store = store_with()
    store.latest.side_effect = history.get
    client = publication_client()
    client.publish(store, version)
    text = client.call.call_args_list[1].args[1]["initial_comment"]
    assert "Skin Elixir" in text
    assert "Mini Sea Salt Spray" in text
    assert "Target retail Sea Salt Spray" in text
    assert "4.0/day" in text
    assert "Product" in text  # A positive unregistered SKU must remain visible.


def store_with(receipt=None):
    store = Mock(spec=Store)
    store.receipts.return_value = {"slack": receipt} if receipt else {}
    return store


def test_ambiguous_complete_response_is_reconciled_without_duplicate_upload():
    store = store_with(
        {"status": "pending", "digest": digest(verified().report), "provider_id": "F123"}
    )
    client = SlackReport("token", "C123")
    client.info = Mock(
        return_value={
            "id": "F123",
            "channels": ["C123"],
            "size": len(verified().report),
            "url_private_download": "https://files.slack.com/file",
        }
    )
    client.download = Mock(return_value=verified().report)
    client.call = Mock()
    assert client.publish(store, verified()) == "F123"
    client.call.assert_not_called()
    assert store.record_receipt.call_args.args[1] == "slack"


def test_unverifiable_pending_file_blocks_reupload():
    store = store_with(
        {"status": "pending", "digest": digest(verified().report), "provider_id": "F123"}
    )
    client = SlackReport("token", "C123")
    client.info = Mock(side_effect=SlackReportError("file unavailable"))
    client.call = Mock()
    with pytest.raises(SlackReportError, match="ambiguous"):
        client.publish(store, verified())
    client.call.assert_not_called()


def test_file_download_never_sends_bot_token_to_another_origin():
    client = SlackReport("token", "C123", session=Mock())
    with pytest.raises(ValueError, match="Slack file origin"):
        client.download("https://evil.example/leak")
    client.session.get.assert_not_called()


def test_api_failure_is_redacted():
    session = Mock()
    session.post.side_effect = requests.Timeout("private credential")
    with pytest.raises(SlackReportError) as error:
        SlackReport("token", "C123", session=session).call("files.info", {"file": "F123"})
    assert "private credential" not in str(error.value)


def test_delayed_file_readback_retries_same_file_without_reupload(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    client = publication_client()
    client.verify.side_effect = [SlackReportError("temporary readback failure"), None]
    store = store_with()
    assert client.publish(store, verified(), summary_text="Report") == "F123"
    assert client.verify.call_count == 2
    assert [call.args[0] for call in client.call.call_args_list] == [
        "files.getUploadURLExternal",
        "files.completeUploadExternal",
    ]
    assert store.record_receipt.call_args.kwargs.get("status", "verified") == "verified"


def test_pending_delivery_retries_then_preserves_receipt(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    client = SlackReport("token", "C123")
    client.verify = Mock(side_effect=SlackReportError("temporary readback failure"))
    client.call = Mock()
    store = store_with({"status": "pending", "provider_id": "F123"})
    with pytest.raises(SlackReportError, match="ambiguous"):
        client.publish(store, verified())
    assert client.verify.call_count == 5
    client.call.assert_not_called()
    store.record_receipt.assert_not_called()
