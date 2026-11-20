"""The production archive and send receipt survive fresh database sessions."""

import os

import pytest

from based_inventory.pipeline.slack_report import SlackReport, SlackReportError
from based_inventory.velocity_archive import VelocityArchive
from tests.test_pipeline_extract import package
from tests.test_pipeline_models import verified
from tests.test_post_velocity_to_slack import SlackServer, pvs


@pytest.fixture
def database_url():
    dsn = os.getenv("TEST_PIPELINE_DATABASE_URL")
    if not dsn:
        pytest.skip("dedicated PostgreSQL test database required")
    with VelocityArchive(dsn) as archive:
        archive._connection.execute("TRUNCATE daily_velocity_archive")
    return dsn


def test_sent_day_and_receipt_survive_close_reopen_and_replay_uploads_nothing(database_url):
    server = SlackServer()
    version = verified()
    with VelocityArchive(database_url) as archive, archive.lock(version.day):
        saved = archive.save(version)
        SlackReport("test-token", "C_TEST", session=server).publish(
            archive.delivery_store(saved, "C_TEST"), saved, summary_text="Reviewed daily report"
        )
    assert server.reservations == 1
    assert len(server.comments) == 1

    class ReadbackOnly:
        def post(self, url, **kwargs):
            assert url.endswith("files.info"), "a replay tried to reserve or upload another file"
            return server.post(url, **kwargs)

        def get(self, url, **kwargs):
            return server.get(url, **kwargs)

    # A fresh archive means a new PostgreSQL connection and no process-local cache.
    with VelocityArchive(database_url) as reopened, reopened.lock(version.day):
        assert reopened.window(version.day)[version.day] == version
        saved = reopened.save(verified(report=b"regenerated identical sales workbook"))
        receipts = reopened.delivery_store(saved, "C_TEST").receipts(saved.version_id)
        assert receipts["slack"]["status"] == "verified"
        SlackReport("test-token", "C_TEST", session=ReadbackOnly()).publish(
            reopened.delivery_store(saved, "C_TEST"), saved, summary_text="Reviewed daily report"
        )
    assert server.reservations == 1
    assert len(server.comments) == 1


def test_pending_receipt_survives_process_restart_and_blocks_blind_reupload(database_url):
    server = SlackServer("ambiguous")
    version = verified()
    with VelocityArchive(database_url) as archive, archive.lock(version.day):
        archive.save(version)
        with pytest.raises(SlackReportError, match="ambiguous"):
            SlackReport("test-token", "C_TEST", session=server).publish(
                archive.delivery_store(version, "C_TEST"), version, summary_text="Daily report"
            )
    with VelocityArchive(database_url) as reopened, reopened.lock(version.day):
        saved = reopened.latest(version.day)
        assert (
            reopened.delivery_store(saved, "C_TEST").receipts(saved.version_id)["slack"]["status"]
            == "pending"
        )
        with pytest.raises(SlackReportError, match="ambiguous"):
            SlackReport("test-token", "C_TEST", session=server).publish(
                reopened.delivery_store(saved, "C_TEST"), saved, summary_text="Daily report"
            )
    assert server.reservations == 1
    assert len(server.comments) == 1


def test_database_lock_is_shared_between_process_connections_and_released_on_close(database_url):
    day = verified().day
    with (
        VelocityArchive(database_url) as first,
        VelocityArchive(database_url) as second,
        first.lock(day),
        pytest.raises(RuntimeError, match="already running"),
        second.lock(day),
    ):
        pytest.fail("competing connection obtained the daily send lock")
    with VelocityArchive(database_url) as reopened, reopened.lock(day):
        reopened.save(verified())


def test_database_archive_is_immutable_and_proof_is_checked_on_fresh_read(database_url):
    version = verified()
    with VelocityArchive(database_url) as first:
        first.save(version)
    with VelocityArchive(database_url) as reopened:
        with pytest.raises(ValueError, match="different verified sales"):
            reopened.save(verified(excluded_orders={}))
        assert reopened.latest(version.day) == version
        reopened._connection.execute(
            "UPDATE daily_velocity_archive SET payload=jsonb_set(payload, '{report_base64}', %s::jsonb) WHERE archive_key=%s",
            ('"dGFtcGVyZWQ="', f"day:{version.day}"),
        )
    with VelocityArchive(database_url) as reopened, pytest.raises(ValueError, match="hash"):
        reopened.latest(version.day)


def test_production_cli_uses_explicit_database_secret_and_preserves_delivery_on_reopen(
    database_url, tmp_path, monkeypatch
):
    stem, _ = package(tmp_path)
    server = SlackServer()
    monkeypatch.setattr(pvs, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "b" * 40)
    monkeypatch.setenv("VELOCITY_DATABASE_URL", database_url)
    monkeypatch.setenv("STATE_PATH", "redis://unused-alert-state")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setattr("requests.Session", lambda: server)
    args = ["--xlsx", str(stem.with_suffix(".xlsx")), "--day", "2026-11-16", "--channel", "C_TEST"]
    assert pvs.main(args) == 0
    assert pvs.main(args) == 0
    assert server.reservations == 1
    with VelocityArchive(database_url) as reopened:
        saved = reopened.latest(verified().day)
        assert saved.totals["Shopify"] == 4
        assert (
            reopened.delivery_store(saved, "C_TEST").receipts(saved.version_id)["slack"]["status"]
            == "verified"
        )


def test_all_channel_report_sources_and_exact_delivery_survive_database_reopen(database_url):
    from based_inventory.sales_report_archive import SalesReportArchive
    from tests.test_all_channel_sales import model
    from tests.test_post_sales_velocity import job

    source = model()
    server = SlackServer()
    with VelocityArchive(database_url) as storage, storage.lock(source.day):
        archive = SalesReportArchive(storage)
        archive.save_day(source)
        frozen = archive.save_package(job._package(source, archive.window(source.day)))
        SlackReport("test-token", "C_TEST", session=server).publish(
            archive.delivery_store(frozen, "C_TEST"), frozen, summary_text=frozen.summary
        )
    with VelocityArchive(database_url) as storage, storage.lock(source.day):
        archive = SalesReportArchive(storage)
        assert archive.latest_day(source.day).capture_id == source.capture_id
        saved = archive.latest_package(source.day)
        assert saved.version_id == frozen.version_id
        assert saved.summary == frozen.summary
        assert saved.report == frozen.report
        SlackReport("test-token", "C_TEST", session=server).publish(
            archive.delivery_store(saved, "C_TEST"), saved, summary_text=saved.summary
        )
        assert (
            archive.delivery_store(saved, "C_TEST").receipts(saved.version_id)["slack"]["status"]
            == "verified"
        )
    assert server.reservations == 1
    assert len(server.comments) == 1
