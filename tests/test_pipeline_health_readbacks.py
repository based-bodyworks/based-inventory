from unittest.mock import Mock, patch

from based_inventory.pipeline.health_readbacks import native_readbacks
from tests.test_pipeline_models import verified


def test_monitor_reads_saved_sources_without_collecting_live_stock():
    version = verified()
    store, google, slack = Mock(), Mock(), Mock()
    store.receipts.return_value = {
        "report_archive": {"provider_id": "report"},
        "slack": {"provider_id": "file"},
    }
    plan = {"history_after": [[1]], "rolling_after": [[2]], "daily_after": [[3]]}
    store.connection.execute.return_value.fetchone.return_value = {"plan": plan}
    with (
        patch("based_inventory.pipeline.health_readbacks.MerchantPublisher") as publisher,
        patch("based_inventory.pipeline.health_readbacks.DriveArchive") as archive,
        patch("based_inventory.pipeline.health_readbacks.verify_sources") as sources,
    ):
        publisher.return_value.collect.return_value = {
            "history": [[1]],
            "rolling": [[2]],
            "daily": [[3]],
        }
        archive.return_value.download.return_value = version.report
        sources.return_value = dict.fromkeys(("target", "amazon", "inventory", "workbook"), True)
        result = native_readbacks(store, google, slack)(version)
        assert all(result.values())
        sources.assert_called_once_with(store, version.version_id, google)
        assert not google.batch.called
        slack.verify.assert_called_once_with("file", version)
        assert not store.plan_once.called


def test_changed_native_values_cannot_pass_using_saved_success_receipts():
    version = verified()
    store, google, slack = Mock(), Mock(), Mock()
    store.receipts.return_value = {}
    store.connection.execute.return_value.fetchone.return_value = {
        "plan": {"history_after": [[1]], "rolling_after": [[2]], "daily_after": [[3]]}
    }
    with (
        patch("based_inventory.pipeline.health_readbacks.MerchantPublisher") as publisher,
        patch("based_inventory.pipeline.health_readbacks.verify_sources", return_value={}),
    ):
        publisher.return_value.collect.return_value = {
            "history": [[999]],
            "rolling": [[2]],
            "daily": [[3]],
        }
        result = native_readbacks(store, google, slack)(version)
        assert not result["history"]
        assert not result["slack"]
        assert not result["report_archive"]
        assert not result["target"]
