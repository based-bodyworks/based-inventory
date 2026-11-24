"""Exercise the active send with verified artifacts and an offline Slack server."""

from __future__ import annotations

import csv
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import requests
from psycopg import OperationalError

from based_inventory.velocity_archive import VelocityArchive
from tests.test_pipeline_extract import package
from tests.test_pipeline_models import verified

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import post_velocity_to_slack as pvs  # noqa: E402


@pytest.fixture
def report(tmp_path, monkeypatch):
    stem, _ = package(tmp_path)
    monkeypatch.setattr(pvs, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("RENDER_GIT_COMMIT", "b" * 40)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("STATE_PATH", raising=False)
    monkeypatch.delenv("VELOCITY_HISTORY_PATH", raising=False)
    monkeypatch.delenv("VELOCITY_DATABASE_URL", raising=False)
    return stem, ["--xlsx", str(stem.with_suffix(".xlsx")), "--day", "2026-11-16"]


class Response:
    status_code = 200

    def __init__(self, payload=None, content=b""):
        self.payload, self.content = payload, content

    def json(self):
        return self.payload


class SlackServer:
    """The actual uploader talks to this HTTP-shaped fixture, never the network."""

    def __init__(self, mode="success"):
        self.mode = mode
        self.reservations = 0
        self.completed = False
        self.report = b""
        self.comments = []
        self.titles = []

    def post(self, url, *, data=None, json=None, **kwargs):
        if url.endswith("files.getUploadURLExternal"):
            self.reservations += 1
            return Response(
                {"ok": True, "file_id": "F123", "upload_url": "https://files.slack.com/upload"}
            )
        if url == "https://files.slack.com/upload":
            self.report = data
            return Response()
        if url.endswith("files.completeUploadExternal"):
            self.comments.append(json["initial_comment"])
            self.titles.append(json["files"][0]["title"])
            if self.mode != "ambiguous":
                self.completed = True
            if self.mode in {"ambiguous", "lost-success"}:
                raise requests.Timeout("private network detail")
            return Response({"ok": True})
        if url.endswith("files.info"):
            return Response(
                {
                    "ok": True,
                    "file": {
                        "channels": ["C_TEST"] if self.completed else [],
                        "size": len(self.report),
                        "url_private_download": "https://files.slack.com/download",
                    },
                }
            )
        raise AssertionError("Unexpected publication request: " + url)

    def get(self, url, **kwargs):
        assert url == "https://files.slack.com/download"
        return Response(content=self.report)


def test_preview_needs_no_credentials_or_network_and_shows_every_catalog_row(
    report, monkeypatch, capsys
):
    _, args = report
    monkeypatch.setattr(requests, "Session", lambda: pytest.fail("preview opened Slack"))
    monkeypatch.setattr("redis.from_url", lambda *a, **k: pytest.fail("preview opened Redis"))
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: pytest.fail("preview opened database"))
    monkeypatch.setenv("STATE_PATH", "redis://not-a-preview-source")
    monkeypatch.setenv("VELOCITY_DATABASE_URL", "postgresql://not-a-preview-source")
    assert pvs.main([*args, "--preview"]) == 0
    text = capsys.readouterr().out
    assert "47 tracked SKUs" in text
    assert "2026-11-16 (UTC)" in text
    assert "1/7 verified days" in text
    assert "Tallow Moisturizer (Travel): *0* | n/a" in text
    assert "Product (`sku`): *4* | n/a" in text


def test_dry_run_never_opens_remote_history_or_sends_slack(report, monkeypatch, capsys):
    _, args = report
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("STATE_PATH", "redis://not-a-preview-source")
    monkeypatch.setenv("VELOCITY_DATABASE_URL", "postgresql://not-a-preview-source")
    monkeypatch.setattr(requests, "Session", lambda: pytest.fail("dry run opened Slack"))
    monkeypatch.setattr("redis.from_url", lambda *a, **k: pytest.fail("dry run opened Redis"))
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: pytest.fail("dry run opened database"))
    assert pvs.main(args) == 0
    assert "Daily SKU Velocity" in capsys.readouterr().out


def test_preview_can_read_only_explicit_local_verified_history(report, tmp_path, capsys):
    _, args = report
    history = tmp_path / "history"
    with VelocityArchive(history) as archive:
        for offset in range(1, 7):
            archive.save(verified(day=verified().day - timedelta(days=offset)))
    assert pvs.main([*args, "--preview", "--history", str(history)]) == 0
    text = capsys.readouterr().out
    assert "7/7 verified days" in text
    assert "Product (`sku`): *4* | 4.0/day" in text
    assert not any(p.name.endswith("2026-11-16.json") for p in history.glob("day*.json"))


@pytest.mark.parametrize("location", ["redis://not-allowed", "postgresql://not-allowed"])
def test_preview_refuses_an_explicit_remote_history_source(report, capsys, location):
    _, args = report
    assert pvs.main([*args, "--preview", "--history", location]) == 1
    assert "local history" in capsys.readouterr().err


@pytest.mark.parametrize("damage", ["missing-proof", "wrong-day", "multi-day"])
def test_source_artifact_errors_block_before_any_publication(report, monkeypatch, damage):
    stem, args = report
    if damage == "missing-proof":
        stem.with_suffix(".checkpoint.json").unlink()
    elif damage == "wrong-day":
        args[-1] = "2026-11-15"
    else:
        with stem.with_suffix(".csv").open("a", newline="") as file:
            csv.writer(file).writerow(["2026-11-15", "Product", "SHOPIFY", 999, "sku"])
    monkeypatch.setattr(requests, "Session", lambda: pytest.fail("invalid source opened Slack"))
    assert pvs.main([*args, "--preview"]) == 1


def test_production_requires_a_durable_history_backend(report, tmp_path, monkeypatch, capsys):
    _, args = report
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setattr(requests, "Session", lambda: pytest.fail("invalid config opened Slack"))
    assert pvs.main([*args, "--history", str(tmp_path / "ephemeral")]) == 1
    assert "PostgreSQL" in capsys.readouterr().err


def test_production_refuses_redis_instead_of_claiming_durable_history(report, monkeypatch, capsys):
    _, args = report
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setenv("VELOCITY_DATABASE_URL", "redis://nonpersistent-service")
    monkeypatch.setattr(requests, "Session", lambda: pytest.fail("invalid config opened Slack"))
    assert pvs.main(args) == 1
    assert "PostgreSQL" in capsys.readouterr().err


def test_database_failure_stops_delivery_without_exposing_the_secret(report, monkeypatch, capsys):
    _, args = report
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setenv("VELOCITY_DATABASE_URL", "postgresql://private-connection")

    def unavailable(_):
        raise OperationalError("private-connection with credentials")

    monkeypatch.setattr(pvs, "VelocityArchive", unavailable)
    monkeypatch.setattr(
        requests, "Session", lambda: pytest.fail("unavailable archive opened Slack")
    )
    assert pvs.main(args) == 1
    text = capsys.readouterr().err
    assert "PostgreSQL velocity archive is unavailable" in text
    assert "private-connection" not in text


def test_verified_send_saves_the_day_and_does_not_repeat_a_successful_upload(
    report, tmp_path, monkeypatch
):
    _, args = report
    server = SlackServer()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setattr(requests, "Session", lambda: server)
    args += ["--channel", "C_TEST", "--history", str(tmp_path / "history")]
    assert pvs.main(args) == 0
    assert pvs.main(args) == 0
    assert server.reservations == 1
    assert len(server.comments) == 1
    assert "47 tracked SKUs" in server.comments[0]
    assert "Other sold SKUs (mapping unverified)" in server.comments[0]


def test_lost_success_is_reconciled_from_the_reserved_file(report, tmp_path, monkeypatch):
    _, args = report
    server = SlackServer("lost-success")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setattr(requests, "Session", lambda: server)
    args += ["--channel", "C_TEST", "--history", str(tmp_path / "history")]
    assert pvs.main(args) == 0
    assert pvs.main(args) == 0
    assert server.reservations == 1


def test_ambiguous_upload_never_falls_back_or_reserves_a_second_file(
    report, tmp_path, monkeypatch, capsys
):
    _, args = report
    server = SlackServer("ambiguous")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setattr(requests, "Session", lambda: server)
    args += ["--channel", "C_TEST", "--history", str(tmp_path / "history")]
    assert pvs.main(args) == 1
    assert pvs.main(args) == 1
    assert server.reservations == 1
    assert "ambiguous" in capsys.readouterr().err


def test_archive_only_import_needs_no_slack_and_retains_verified_source(
    report, tmp_path, monkeypatch
):
    _, args = report
    history = tmp_path / "history"
    monkeypatch.setattr(requests, "Session", lambda: pytest.fail("archive import opened Slack"))
    assert pvs.main([*args, "--archive-only", "--history", str(history)]) == 0
    with VelocityArchive(history) as archive:
        assert archive.latest(verified().day).totals["Shopify"] == 4
