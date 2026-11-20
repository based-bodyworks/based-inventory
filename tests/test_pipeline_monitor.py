from datetime import UTC, date, datetime, timedelta

import pytest

from based_inventory.pipeline.models import Coverage, SalesRow, VerifiedDay, digest
from based_inventory.pipeline.monitor import (
    alert_identity,
    check_pipeline,
    expected_reporting_day,
    format_alert,
)

DAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 17, 16, 45, tzinfo=UTC)
DESTINATIONS = (
    "report_archive",
    "history",
    "canonical",
    "rolling",
    "erp",
    "inventory",
    "slack",
    "target",
    "amazon",
    "workbook",
)


def saved_day():
    return VerifiedDay(
        DAY,
        (SalesRow("sku", "Product", "Shopify", 1),),
        Coverage(1, 1, 1, True),
        {},
        "a" * 64,
        "b" * 40,
        b"workbook",
        NOW - timedelta(hours=5),
    )


class Store:
    def __init__(self):
        self.version = saved_day()
        self.runs = {
            kind: dict(
                status="verified",
                version_id=self.version.version_id,
                started_at=NOW - timedelta(hours=1),
                updated_at=NOW,
            )
            for kind in ("sales", "trackers")
        }
        self.saved_receipts = {
            d: dict(
                status="verified",
                digest=(
                    digest(self.version.report) if d in ("report_archive", "slack") else "c" * 64
                ),
                provider_id="file",
            )
            for d in DESTINATIONS
        }

    def latest(self, day):
        return self.version

    def latest_run(self, kind, day):
        return self.runs.get(kind)

    def receipts(self, version_id):
        return self.saved_receipts


def native(version):
    return dict.fromkeys(DESTINATIONS, True)


@pytest.mark.parametrize(
    "instant, expected",
    [
        ("2026-03-08T16:45:00+00:00", date(2026, 3, 7)),
        ("2026-11-01T17:45:00+00:00", date(2026, 10, 31)),
        ("2026-11-17T07:59:00+00:00", date(2026, 11, 15)),
        ("2026-11-17T08:00:00+00:00", DAY),
    ],
)
def test_expected_day_tracks_pacific_calendar_through_dst(instant, expected):
    assert expected_reporting_day(datetime.fromisoformat(instant)) == expected


def test_naive_clock_is_rejected():
    with pytest.raises(ValueError):
        expected_reporting_day(datetime(2026, 11, 17))


def test_complete_native_readbacks_and_runs_are_healthy():
    report = check_pipeline(DAY, Store(), native, now_utc=NOW)
    assert report.state == "healthy"
    assert report.reason_codes == ()


def test_job_that_never_started_fails_after_deadline():
    store = Store()
    store.runs = {}
    store.version = None
    report = check_pipeline(DAY, store, native, now_utc=NOW)
    assert report.state == "failed"
    assert "sales_not_started" in report.reason_codes


def test_missing_job_before_deadline_is_not_a_failure():
    store = Store()
    store.runs = {}
    store.version = None
    assert (
        check_pipeline(DAY, store, native, now_utc=NOW - timedelta(minutes=1)).state == "incomplete"
    )


def test_extraction_failure_cannot_be_hidden_by_old_verified_files():
    store = Store()
    store.runs["sales"]["status"] = "failed"
    store.runs["sales"]["error_code"] = "source_count_changed"
    report = check_pipeline(DAY, store, native, now_utc=NOW)
    assert report.state == "failed"
    assert "sales_failed" in report.reason_codes


@pytest.mark.parametrize(
    "started, heartbeat, state", [(10, 1, "running"), (30, 25, "stale"), (181, 1, "stale")]
)
def test_running_job_requires_recent_heartbeat_and_bounded_duration(started, heartbeat, state):
    store = Store()
    store.runs["sales"].update(
        status="running",
        started_at=NOW - timedelta(minutes=started),
        updated_at=NOW - timedelta(minutes=heartbeat),
    )
    assert check_pipeline(DAY, store, native, now_utc=NOW).state == state


@pytest.mark.parametrize("destination", DESTINATIONS)
def test_missing_receipts_prevent_success(destination):
    store = Store()
    del store.saved_receipts[destination]
    report = check_pipeline(DAY, store, native, now_utc=NOW)
    assert report.state == "incomplete"
    assert f"{destination}_receipt_missing" in report.reason_codes


def test_wrong_report_hash_prevents_success():
    store = Store()
    store.saved_receipts["slack"]["digest"] = "0" * 64
    report = check_pipeline(DAY, store, native, now_utc=NOW)
    assert report.state == "incomplete"
    assert "slack_report_hash_mismatch" in report.reason_codes


def test_stale_native_version_prevents_success():
    report = check_pipeline(DAY, Store(), lambda _: {**native(None), "history": False}, now_utc=NOW)
    assert report.state == "stale"
    assert "history_readback_mismatch" in report.reason_codes


@pytest.mark.parametrize("destination", ["target", "amazon", "workbook"])
def test_incomplete_separate_source_cannot_look_healthy(destination):
    store = Store()
    store.saved_receipts[destination]["status"] = "pending"
    assert check_pipeline(DAY, store, native, now_utc=NOW).state == "incomplete"


def test_tracker_run_must_match_current_sales_version():
    store = Store()
    store.runs["trackers"]["version_id"] = "old"
    report = check_pipeline(DAY, store, native, now_utc=NOW)
    assert report.state == "stale"
    assert "trackers_version_mismatch" in report.reason_codes


def test_callback_failure_is_redacted_and_not_healthy():
    def broken(_):
        raise RuntimeError("secret-token")

    report = check_pipeline(DAY, Store(), broken, now_utc=NOW)
    assert report.state == "incomplete"
    assert "provider_readback_failed" in report.reason_codes
    assert "secret-token" not in report.message


def test_missing_callback_is_not_healthy():
    assert check_pipeline(DAY, Store(), None, now_utc=NOW).state == "incomplete"


def test_alert_identity_is_stable_and_template_has_no_slack_mentions():
    store = Store()
    store.runs["sales"]["status"] = "failed"
    report = check_pipeline(DAY, store, native, now_utc=NOW)
    assert alert_identity(report) == alert_identity(
        check_pipeline(DAY, store, native, now_utc=NOW + timedelta(minutes=2))
    )
    text = format_alert(report)
    assert DAY.isoformat() in text
    assert "failed" in text
    assert "<!" not in text


def test_healthy_has_no_alert_identity():
    assert alert_identity(check_pipeline(DAY, Store(), native, now_utc=NOW)) is None


@pytest.mark.parametrize(
    "day, before, after",
    [
        (date(2026, 3, 7), "2026-03-08T15:44:00+00:00", "2026-03-08T15:45:00+00:00"),
        (date(2026, 10, 31), "2026-11-01T16:44:00+00:00", "2026-11-01T16:45:00+00:00"),
    ],
)
def test_deadline_is_0845_pacific_on_dst_transition_days(day, before, after):
    store = Store()
    store.version = None
    store.runs = {}
    assert (
        check_pipeline(day, store, native, now_utc=datetime.fromisoformat(before)).state
        == "incomplete"
    )
    assert (
        check_pipeline(day, store, native, now_utc=datetime.fromisoformat(after)).state == "failed"
    )


def test_active_sales_job_does_not_hide_tracker_that_never_started():
    store = Store()
    store.runs["sales"].update(status="running", started_at=NOW - timedelta(minutes=10))
    del store.runs["trackers"]
    report = check_pipeline(DAY, store, native, now_utc=NOW)
    assert report.state == "failed"
    assert "trackers_not_started" in report.reason_codes


def test_native_readback_omissions_are_not_success():
    report = check_pipeline(DAY, Store(), lambda _: {}, now_utc=NOW)
    assert report.state == "stale"
    assert "slack_readback_mismatch" in report.reason_codes


def test_database_error_is_not_silently_changed_into_success():
    store = Store()

    def broken(_):
        raise RuntimeError("database unavailable")

    store.latest = broken
    with pytest.raises(RuntimeError, match="database unavailable"):
        check_pipeline(DAY, store, native, now_utc=NOW)


def test_native_monitor_requires_warehouse_job_and_skips_only_merchant_slack():
    store = Store()
    store.runs["warehouse"] = store.runs.pop("sales")
    store.saved_receipts.pop("slack")

    def warehouse_native(version):
        return {key: value for key, value in native(version).items() if key != "slack"}

    report = check_pipeline(DAY, store, warehouse_native, now_utc=NOW, native_report=True)
    assert report.state == "healthy"
    # Losing a real warehouse/source readback still fails the combined monitor.
    store.saved_receipts.pop("history")
    report = check_pipeline(DAY, store, warehouse_native, now_utc=NOW, native_report=True)
    assert report.state == "incomplete" and "history_receipt_missing" in report.reason_codes
