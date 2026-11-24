from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from based_inventory.pipeline.runner import local_time_matches, run_command

DAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 17, 16, 45, tzinfo=UTC)


class Store:
    def __init__(self, version=True):
        self.version = SimpleNamespace(day=DAY, version_id="v") if version else None
        self.events = []
        self.locked = False

    @contextmanager
    def lock_merchant_pull(self):
        self.locked = True
        self.events.append("lock")
        try:
            yield
        finally:
            self.locked = False
            self.events.append("unlock")

    def latest(self, day):
        return self.version

    def load(self, version_id):
        return self.version

    def assert_current(self, version_id):
        self.events.append("current")

    def begin_run(self, kind, day):
        assert self.locked
        self.events.append(kind)
        return "run"

    def heartbeat(self, run_id):
        self.events.append("heartbeat")

    def save_version(self, version):
        assert self.locked
        self.version = version
        self.events.append("save")

    def finish_run(self, run_id, status, **kwargs):
        self.events.append(status)
        if status == "failed":
            self.error_code = kwargs["error_code"]


class Archive:
    def __init__(self, google):
        pass

    def ensure_report(self, store, version):
        assert store.locked
        store.events.append("archive")
        return "source-url"


class Merchant:
    def __init__(self, store, google):
        self.store = store

    def publish(self, version, source_url):
        assert self.store.locked
        self.store.events.append("merchant")


class Slack:
    def publish(self, store, version):
        assert store.locked
        store.events.append("slack")


@pytest.fixture(autouse=True)
def adapters(monkeypatch):
    monkeypatch.setenv("PIPELINE_ALLOW_WRITES", "1")
    monkeypatch.setattr("based_inventory.pipeline.runner.DriveArchive", Archive)
    monkeypatch.setattr("based_inventory.pipeline.runner.MerchantPublisher", Merchant)


def test_sales_reuses_saved_version_and_orders_publications():
    store = Store()
    result = run_command("sales", store=store, google=None, slack=Slack(), day=DAY, now_utc=NOW)
    assert result["status"] == "verified"
    assert (
        store.events.index("archive")
        < store.events.index("merchant")
        < store.events.index("slack")
        < store.events.index("verified")
    )
    assert not store.locked


def test_operations_copies_delivered_report_even_when_warehouse_publication_fails(
    monkeypatch, tmp_path
):
    from contextlib import nullcontext

    from based_inventory.pipeline import platform_trackers, runner
    from based_inventory.velocity_archive import VelocityArchive
    from tests.test_platform_trackers import Google, package

    report = package()
    google = Google()
    for key in (
        "DATABASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN", "SLACK_BOT_TOKEN", "SLACK_REPORT_CHANNEL_ID",
    ):
        monkeypatch.setenv(key, "test")
    monkeypatch.setattr(runner, "Store", lambda _: nullcontext(Store(False)))
    monkeypatch.setattr(runner, "GoogleAPI", lambda *_: google)
    monkeypatch.setattr(runner, "SlackReport", lambda *_: object())
    monkeypatch.setattr(
        "based_inventory.velocity_archive.VelocityArchive", lambda *_, **__: VelocityArchive(tmp_path)
    )
    monkeypatch.setattr(platform_trackers, "report_inputs", lambda *_: (report, {}))

    def copy_report(*_, publish=False):
        assert publish
        with VelocityArchive(tmp_path) as storage:
            platform_trackers.publish_views(storage, report, google)
        return report.version_id

    monkeypatch.setattr(platform_trackers, "check_or_publish", copy_report)

    def warehouse_unavailable(*_, **__):
        raise RuntimeError("warehouse unavailable")

    monkeypatch.setattr(runner, "run_command", warehouse_unavailable)
    assert runner.main(["operations", "--day", str(report.day)]) == 1
    assert len(google.tabs) == 6
    assert all(tab[1][6] == ["Amazon includes FBA", 42] for tab in google.tabs.values())


def test_missing_version_extracts_inside_lock_with_heartbeat():
    store = Store(False)

    def extract(day, *, heartbeat):
        assert store.locked
        heartbeat()
        return SimpleNamespace(day=day, version_id="new")

    run_command(
        "sales", store=store, google=None, slack=Slack(), day=DAY, now_utc=NOW, extractor=extract
    )
    assert store.events.index("heartbeat") < store.events.index("save")


def test_publish_disabled_before_any_run_or_lock(monkeypatch):
    monkeypatch.delenv("PIPELINE_ALLOW_WRITES")
    store = Store()
    with pytest.raises(PermissionError):
        run_command("sales", store=store, google=None, slack=None, day=DAY, now_utc=NOW)
    assert store.events == []


def test_failure_is_recorded_without_secret_text():
    store = Store(False)

    def fail(*args, **kwargs):
        raise RuntimeError("secret-token")

    with pytest.raises(RuntimeError, match="pipeline_failed"):
        run_command(
            "sales", store=store, google=None, slack=None, day=DAY, now_utc=NOW, extractor=fail
        )
    assert store.error_code == "sales_failed"
    assert store.events[-1] == "unlock"


def test_trackers_never_extract_and_incomplete_cannot_finish_verified(monkeypatch):
    store = Store()

    def refresh(*args, **kwargs):
        assert store.locked
        assert kwargs["mode"] == "publish"
        return {"status": "incomplete"}

    monkeypatch.setattr("based_inventory.pipeline.runner.refresh_sources", refresh)
    with pytest.raises(RuntimeError, match="pipeline_failed"):
        run_command("trackers", store=store, google=None, slack=None, day=DAY, now_utc=NOW)
    assert store.error_code == "trackers_failed"


def test_compare_is_readonly_even_when_writes_disabled(monkeypatch):
    monkeypatch.delenv("PIPELINE_ALLOW_WRITES")
    store = Store()
    monkeypatch.setattr(
        "based_inventory.pipeline.runner.check_pipeline",
        lambda *args, **kwargs: SimpleNamespace(
            state="healthy", message="verified", version_id="v", reason_codes=()
        ),
    )
    result = run_command(
        "compare",
        store=store,
        google=None,
        slack=None,
        day=DAY,
        now_utc=NOW,
        provider_readbacks=lambda _: {},
    )
    assert result["status"] == "healthy"
    assert store.events == []


@pytest.mark.parametrize(
    "instant, clock, expected",
    [
        ("2026-03-08T14:00:00+00:00", "07:00", True),
        ("2026-03-08T15:00:00+00:00", "07:00", False),
        ("2026-11-01T14:00:00+00:00", "07:00", False),
        ("2026-11-01T15:00:00+00:00", "07:00", True),
        ("2026-03-08T15:45:00+00:00", "08:45", True),
        ("2026-11-01T16:45:00+00:00", "08:45", True),
    ],
)
def test_dst_candidate_crons_run_once_at_local_clock(instant, clock, expected):
    assert local_time_matches(datetime.fromisoformat(instant), clock) is expected


def test_fresh_reextracts_even_with_saved_version():
    store = Store()

    def extract(day, *, heartbeat):
        return SimpleNamespace(day=day, version_id="new")

    result = run_command(
        "sales",
        store=store,
        google=None,
        slack=Slack(),
        day=DAY,
        fresh=True,
        now_utc=NOW,
        extractor=extract,
    )
    assert result["version_id"] == "new"
    assert "save" in store.events


def test_trackers_missing_sales_version_records_failure_without_extracting():
    store = Store(False)

    def forbidden(*args, **kwargs):
        pytest.fail("trackers must never collect merchant orders")

    with pytest.raises(RuntimeError):
        run_command(
            "trackers",
            store=store,
            google=None,
            slack=None,
            day=DAY,
            now_utc=NOW,
            extractor=forbidden,
        )
    assert store.error_code == "trackers_failed"


def test_local_schedule_rejects_naive_or_invalid_clock():
    with pytest.raises(ValueError):
        local_time_matches(datetime(2026, 11, 17), "07:00")
    with pytest.raises(ValueError):
        local_time_matches(NOW, "25:00")


def test_cli_skip_never_opens_database(monkeypatch, capsys):
    from based_inventory.pipeline.runner import main

    monkeypatch.setattr("based_inventory.pipeline.runner.local_time_matches", lambda *a: False)
    monkeypatch.setattr(
        "based_inventory.pipeline.runner.Store", lambda *a: pytest.fail("must skip database")
    )
    assert main(["sales", "--at-local-time", "07:00"]) == 0
    assert "outside_local_schedule" in capsys.readouterr().out


def test_cli_configuration_failure_is_redacted(monkeypatch, capsys):
    from based_inventory.pipeline.runner import main

    monkeypatch.setenv("DATABASE_URL", "secret-dsn")

    def broken(*args):
        raise RuntimeError("secret-dsn")

    monkeypatch.setattr("based_inventory.pipeline.runner.Store", broken)
    assert main(["monitor"]) == 1
    assert "secret-dsn" not in capsys.readouterr().out


def test_cli_unhealthy_monitor_exits_nonzero(monkeypatch, capsys):
    import sys

    from based_inventory.pipeline.runner import main

    class Connection:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setenv("DATABASE_URL", "configured")
    for key in (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "SLACK_BOT_TOKEN",
        "SLACK_REPORT_CHANNEL_ID",
    ):
        monkeypatch.setenv(key, "configured")
    monkeypatch.setattr("based_inventory.pipeline.runner.Store", Connection)
    monkeypatch.setitem(
        sys.modules,
        "based_inventory.pipeline.health_readbacks",
        SimpleNamespace(native_readbacks=lambda *args: lambda _: {}),
    )
    monkeypatch.setattr(
        "based_inventory.pipeline.runner.run_command",
        lambda *args, **kwargs: {"status": "incomplete", "reason_codes": ["sales_missing"]},
    )
    assert main(["monitor"]) == 1
    assert "sales_missing" in capsys.readouterr().out


@pytest.mark.parametrize(
    "instant, clock, expected",
    [
        ("2026-03-08T14:01:00+00:00", "07:00", True),
        ("2026-11-01T15:01:00+00:00", "07:00", True),
        ("2026-03-08T15:46:00+00:00", "08:45", True),
        ("2026-11-01T16:46:00+00:00", "08:45", True),
        ("2026-03-08T14:59:59+00:00", "07:00", True),
        ("2026-03-08T15:00:00+00:00", "07:00", False),
        ("2026-03-08T13:59:59+00:00", "07:00", False),
        ("2026-11-01T15:59:59+00:00", "07:00", True),
        ("2026-11-01T16:00:00+00:00", "07:00", False),
        ("2026-11-01T14:59:59+00:00", "07:00", False),
        ("2026-03-08T16:44:59+00:00", "08:45", True),
        ("2026-03-08T16:45:00+00:00", "08:45", False),
        ("2026-11-01T16:44:59+00:00", "08:45", False),
        ("2026-11-01T17:45:00+00:00", "08:45", False),
    ],
)
def test_local_schedule_allows_under_one_hour_startup_delay(instant, clock, expected):
    assert local_time_matches(datetime.fromisoformat(instant), clock) is expected


def test_warehouse_command_never_publishes_merchant_slack_report():
    store = Store()

    class NoSend:
        def publish(self, *args, **kwargs):
            pytest.fail("Warehouse job attempted a second Slack report")

    result = run_command(
        "warehouse", store=store, google=None, slack=NoSend(), day=DAY, now_utc=NOW
    )
    assert result["status"] == "verified"
    assert "warehouse" in store.events and "merchant" in store.events and "archive" in store.events
    assert "slack" not in store.events
