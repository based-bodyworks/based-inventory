import os
from dataclasses import replace
from datetime import timedelta

import pytest

from based_inventory.pipeline.store import BusyPipelineError, Store
from tests.test_pipeline_models import verified


@pytest.fixture
def stores():
    dsn = os.getenv("TEST_PIPELINE_DATABASE_URL")
    if not dsn:
        pytest.skip("dedicated PostgreSQL test database required")
    with Store(dsn) as first, Store(dsn) as second:
        first.migrate()
        first.connection.execute(
            "TRUNCATE sales_versions, pipeline_receipts, pipeline_runs CASCADE"
        )
        yield first, second


def test_database_lock_prevents_competing_extraction_and_releases_on_failure(stores):
    first, second = stores
    with first.lock_merchant_pull(), pytest.raises(BusyPipelineError), second.lock_merchant_pull():
        pass
    with second.lock_merchant_pull():
        pass
    with pytest.raises(RuntimeError), first.lock_merchant_pull():
        raise RuntimeError("interrupted extraction")
    with second.lock_merchant_pull():
        pass


def test_version_is_immutable_retryable_and_stale_publication_is_blocked(stores):
    first, _ = stores
    version = verified()
    assert first.save_version(version) == first.save_version(version)
    assert first.load(version.version_id) == version
    first.record_receipt(version.version_id, "history", "c" * 64, "saved-row-set")
    assert first.receipts(version.version_id)["history"]["provider_id"] == "saved-row-set"
    newer = replace(version, captured_at=version.captured_at + timedelta(hours=1))
    first.save_version(newer)
    assert first.latest(version.day).version_id == newer.version_id
    with pytest.raises(ValueError, match="newer"):
        first.record_receipt(version.version_id, "rolling", "d" * 64, "old-write")


def test_saved_blob_corruption_and_no_run_are_visible(stores):
    first, _ = stores
    assert first.latest(verified().day) is None
    run = first.begin_run("sales", verified().day)
    assert first.latest_run("sales", verified().day)["status"] == "running"
    first.finish_run(run, "failed", error_code="extract_timeout")
    assert first.latest_run("sales", verified().day)["error_code"] == "extract_timeout"
    version = verified()
    first.save_version(version)
    first.connection.execute("UPDATE sales_versions SET report = %s", (b"bad workbook",))
    with pytest.raises(ValueError, match="report hash"):
        first.load(version.version_id)


def test_later_source_day_fences_old_stock_replay_before_receipt_exists(stores):
    first, _ = stores
    old = verified()
    newer = replace(
        old, day=old.day + timedelta(days=1), captured_at=old.captured_at + timedelta(days=1)
    )
    first.save_version(old)
    first.save_version(newer)
    first.assert_destination_current(old.version_id, "inventory")
    first.plan_once(newer.version_id, "inventory", {"captured": "newer stock"})
    with pytest.raises(ValueError, match="later reporting day"):
        first.assert_destination_current(old.version_id, "inventory")
    first.assert_destination_current(newer.version_id, "inventory")
    first.assert_destination_current(old.version_id, "target")
