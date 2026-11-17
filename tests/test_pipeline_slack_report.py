from unittest.mock import Mock

import pytest
import requests

from based_inventory.pipeline.models import digest
from based_inventory.pipeline.slack_report import SlackReport, SlackReportError
from based_inventory.pipeline.store import Store
from tests.test_pipeline_models import verified


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
