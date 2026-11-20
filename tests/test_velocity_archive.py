import json
from datetime import date, timedelta

import pytest

from based_inventory.pipeline.models import digest
from based_inventory.velocity_archive import VelocityArchive
from tests.test_pipeline_models import verified


def test_archive_round_trip_validates_saved_report_and_does_not_touch_alert_state(tmp_path):
    archive = VelocityArchive(tmp_path)
    original = verified()
    assert archive.save(original) == original
    assert archive.latest(original.day) == original
    path = next(tmp_path.glob("day*.json"))
    record = json.loads(path.read_text())
    record["report_base64"] = "dGFtcGVyZWQ="
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="hash"):
        archive.latest(original.day)


def test_same_sales_rerun_reuses_first_verified_report_and_conflicting_sales_stop(tmp_path):
    archive = VelocityArchive(tmp_path)
    original = verified()
    archive.save(original)
    rerun = verified(report=b"same data, newly generated workbook")
    assert archive.save(rerun).version_id == original.version_id
    with pytest.raises(ValueError, match="different verified sales"):
        archive.save(verified(excluded_orders={}))
    assert archive.latest(original.day) == original


def test_archive_window_keeps_missing_dates_missing_and_rejects_misdated_record(tmp_path):
    archive = VelocityArchive(tmp_path)
    end = date(2026, 11, 16)
    for offset in range(6):
        archive.save(verified(day=end - timedelta(days=offset)))
    window = archive.window(end)
    assert len(window) == 6
    assert end - timedelta(days=6) not in window
    path = next(tmp_path.glob("day*.json"))
    record = json.loads(path.read_text())
    record["payload"]["day"] = "2026-10-01"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        archive.window(end)


@pytest.mark.parametrize("location", ["redis://local", "rediss://local"])
def test_nonpersistent_redis_is_never_an_archive_backend(location, monkeypatch):
    monkeypatch.setattr("redis.from_url", lambda *a, **k: pytest.fail("Redis opened"))
    with pytest.raises(ValueError, match="PostgreSQL"):
        VelocityArchive(location)


def test_daily_delivery_receipts_survive_reruns_and_block_a_different_version(tmp_path):
    archive = VelocityArchive(tmp_path)
    version = archive.save(verified())
    delivery = archive.delivery_store(version, "C_TEST")
    delivery.record_receipt(
        version.version_id, "slack", digest(version.report), "F123", status="pending"
    )
    assert delivery.receipts(version.version_id)["slack"]["provider_id"] == "F123"
    with pytest.raises(ValueError, match="report digest"):
        delivery.record_receipt(version.version_id, "slack", "b" * 64, "F123")


def test_file_run_lock_rejects_an_overlapping_send(tmp_path):
    archive = VelocityArchive(tmp_path)
    with (
        archive.lock(verified().day),
        pytest.raises(RuntimeError, match="already running"),
        VelocityArchive(tmp_path).lock(verified().day),
    ):
        pytest.fail("overlapping run acquired the lock")
