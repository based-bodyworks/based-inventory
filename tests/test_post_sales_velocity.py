import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from based_inventory.pipeline.slack_report import SlackReport
from based_inventory.velocity_archive import VelocityArchive
from tests.test_all_channel_sales import model
from tests.test_post_velocity_to_slack import SlackServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import post_sales_velocity as job


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(job, "load_dotenv", lambda *a, **k: None)
    for key in ("ENV", "DRY_RUN", "SLACK_BOT_TOKEN", "VELOCITY_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    source = model()
    source_file = tmp_path / "sources.json"
    source_file.write_text(json.dumps(source.payload()))
    return source, source_file


def test_preview_is_offline_and_saves_the_actual_text_and_workbook(setup, tmp_path, monkeypatch):
    source, source_file = setup
    monkeypatch.setattr(requests, "Session", lambda: pytest.fail("preview opened network"))
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: pytest.fail("preview opened database"))
    monkeypatch.setenv("VELOCITY_DATABASE_URL", "postgresql://production-secret")
    output = tmp_path / "preview"
    assert job.main(["--preview", "--sources", str(source_file), "--out-dir", str(output)]) == 0
    assert (output / f"daily-sales_{source.day}.md").read_text()
    assert (output / f"daily-sales_{source.day}.xlsx").read_bytes().startswith(b"PK")


def test_preview_refuses_to_collect_live_sources(setup, monkeypatch, capsys):
    monkeypatch.setattr(job, "collect_sales_day", lambda *a, **k: pytest.fail("live source read"))
    assert job.main(["--preview", "--day", "2026-11-17"]) == 1
    assert "local" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    "now", [datetime(2026, 11, 18, 15, tzinfo=UTC), datetime(2026, 6, 18, 16, tzinfo=UTC)]
)
def test_other_dst_candidate_exits_before_any_source_or_database_read(monkeypatch, now):
    monkeypatch.setattr(
        job, "load_dotenv", lambda *a, **k: pytest.fail("inactive candidate read config")
    )
    assert job.main(["--at-local-time", "08:00"], now_utc=now) == 0


def test_first_complete_send_is_frozen_and_a_retry_does_not_recollect_or_duplicate(
    setup, tmp_path, monkeypatch
):
    source, _ = setup
    server = SlackServer()
    client = SlackReport("test-token", "C_TEST", session=server)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setenv("SLACK_CHANNEL", "C_TEST")
    monkeypatch.setattr(job, "_clients", lambda: (None, None, None))
    calls = []
    monkeypatch.setattr(job, "collect_sales_day", lambda *a: calls.append(a) or source)
    monkeypatch.setattr(job, "SlackReport", lambda *a, **k: client)
    monkeypatch.setattr(job, "_merchant_context", lambda *a: None)
    args = ["--day", str(source.day), "--history", str(tmp_path / "archive")]
    assert job.main(args) == 0
    assert job.main(args) == 0
    assert len(calls) == 1
    assert server.reservations == 1
    assert len(server.comments) == 1
    assert server.titles == [f"BASED Daily SKU Sales Velocity | {source.day}"]


def test_partial_source_data_never_posts_a_false_all_channel_total(setup, tmp_path, monkeypatch):
    source, _ = setup
    from based_inventory.pipeline.target_refresh import SourceIncomplete

    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setattr(job, "_clients", lambda: (None, None, None))
    monkeypatch.setattr(
        job,
        "collect_sales_day",
        lambda *a: (_ for _ in ()).throw(SourceIncomplete("Target complete dated report missing")),
    )
    monkeypatch.setattr(job, "SlackReport", lambda *a, **k: pytest.fail("partial report published"))
    notices = []
    monkeypatch.setattr("based_inventory.slack.SlackClient", lambda *a: notices.append(a))
    assert job.main(["--day", str(source.day), "--history", str(tmp_path / "archive")]) == 1
    assert not notices
    with VelocityArchive(tmp_path / "archive") as storage:
        assert storage._read(f"sales-package:{source.day}") is None


def test_production_refuses_ephemeral_or_redis_state(setup, tmp_path, monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    for location in (str(tmp_path / "ephemeral"), "redis://free-cache"):
        assert job.main(["--day", "2026-11-17", "--history", location]) == 1


def test_target_search_is_bounded_to_the_requested_report_date(monkeypatch):
    from datetime import date

    monkeypatch.setenv("TARGET_GMAIL_QUERY", 'from:approved@example.com subject:"Sales report"')
    query = job._target_query(date(2026, 11, 17))
    assert 'from:approved@example.com subject:"Sales report"' in query
    assert "after:2026/11/14" in query
    assert "before:2026/11/20" in query


@pytest.mark.parametrize("failures", [1, 2, 3])
def test_transient_source_failures_retry_without_notices_or_duplicate_uploads(
    setup, tmp_path, monkeypatch, failures
):
    from based_inventory.pipeline.target_refresh import SourceIncomplete

    source, _ = setup
    server = SlackServer()
    client = SlackReport("test-token", "C_TEST", session=server)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setattr(job, "_clients", lambda: (None, None, None))
    monkeypatch.setattr(job, "SlackReport", lambda *a, **k: client)
    monkeypatch.setattr(job, "_merchant_context", lambda *a: None)
    monkeypatch.setattr(
        "based_inventory.slack.SlackClient", lambda *a: pytest.fail("pending notice sent")
    )
    delays, calls = [], []
    monkeypatch.setattr("time.sleep", delays.append)

    def collect(*args):
        calls.append(args)
        if len(calls) <= failures:
            raise SourceIncomplete("Online source freshness proof is unavailable")
        return source

    monkeypatch.setattr(job, "collect_sales_day", collect)
    args = ["--day", str(source.day), "--channel", "C_TEST", "--history", str(tmp_path / "archive")]
    assert job.main(args) == (1 if failures == 3 else 0)
    assert len(calls) == min(failures + 1, 3)
    assert delays == [30, 60][: min(failures, 2)]
    assert server.reservations == (0 if failures == 3 else 1)
    if failures < 3:
        assert job.main(args) == 0
        assert server.reservations == 1
        assert len(calls) == failures + 1
    else:
        with VelocityArchive(tmp_path / "archive") as archive:
            assert archive._read(f"sales-day:{source.day}") is None
            assert archive._read(f"sales-package:{source.day}") is None


def test_runtime_with_shared_database_never_falls_back_to_local_history(setup, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://shared-database")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    locations = []

    def open_archive(location):
        locations.append(location)
        raise RuntimeError("stop before opening test database")

    monkeypatch.setattr(job, "VelocityArchive", open_archive)
    assert job.main(["--day", "2026-11-20", "--archive-only"]) == 1
    assert locations == ["postgresql://shared-database"]
