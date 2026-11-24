"""Explicit cloud commands; comparison and monitoring never publish or migrate."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .extract import extract_day
from .google_api import GoogleAPI
from .monitor import check_pipeline, expected_reporting_day
from .publisher import MerchantPublisher
from .report_archive import DriveArchive
from .slack_report import SlackReport
from .source_refresh import refresh_sources
from .store import Store


def local_time_matches(now_utc: datetime, clock: str, timezone="America/Los_Angeles") -> bool:
    """Allow startup within 60 local minutes; exclude the other DST candidate."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("A timezone-aware clock is required")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
        raise ValueError("Local clock must use HH:MM")
    local = now_utc.astimezone(ZoneInfo(timezone))
    hour, minute = map(int, clock.split(":"))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Compare elapsed instants, including seconds: the start is inclusive and
    # exactly one hour late is excluded. Morning cron windows do not cross DST.
    elapsed = now_utc.astimezone(UTC) - scheduled.astimezone(UTC)
    return timedelta(0) <= elapsed < timedelta(hours=1)


def require_writes():
    if os.getenv("PIPELINE_ALLOW_WRITES") != "1":
        raise PermissionError("Cloud pipeline writes are disabled")


def run_command(
    command,
    *,
    store,
    google,
    slack,
    day=None,
    version_id=None,
    fresh=False,
    now_utc=None,
    provider_readbacks=None,
    extractor=None,
    native_report=False,
    frozen_target=None,
):
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A timezone-aware clock is required")
    if command not in {"sales", "warehouse", "trackers", "monitor", "compare"}:
        raise ValueError("Unknown pipeline command")
    day = day or (
        now.astimezone(UTC).date() - timedelta(days=1)
        if command in {"sales", "warehouse"}
        else expected_reporting_day(now)
    )
    if type(day) is not date or day >= now.astimezone(UTC).date():
        raise ValueError("A complete UTC reporting day is required")
    if fresh and (command not in {"sales", "warehouse"} or version_id):
        raise ValueError("Fresh extraction is only available for sales without a version")
    if command in {"monitor", "compare"}:
        if version_id:
            version = store.load(version_id)
            if version.day != day:
                raise ValueError("Saved version does not match reporting day")
            store.assert_current(version_id)
        report = check_pipeline(
            day, store, provider_readbacks, now_utc=now, native_report=native_report
        )
        return {
            "status": report.state,
            "day": day.isoformat(),
            "version_id": report.version_id,
            "reason_codes": list(report.reason_codes),
            "message": report.message,
        }
    require_writes()
    # Session advisory lock covers both collection and every publication destination.
    with store.lock_merchant_pull():
        run_id = store.begin_run(command, day)
        try:
            version = (
                store.load(version_id) if version_id else (None if fresh else store.latest(day))
            )
            if version is None:
                if command not in {"sales", "warehouse"}:
                    raise ValueError("Trackers require a saved verified merchant version")
                version = (extractor or extract_day)(day, heartbeat=lambda: store.heartbeat(run_id))
                store.save_version(version)
            if version.day != day:
                raise ValueError("Saved version does not match reporting day")
            store.assert_current(version.version_id)
            store.heartbeat(run_id)
            if command in {"sales", "warehouse"}:
                source_url = DriveArchive(google).ensure_report(store, version)
                store.heartbeat(run_id)
                MerchantPublisher(store, google).publish(version, source_url)
                store.heartbeat(run_id)
                if command == "sales":
                    slack.publish(store, version)
            else:
                source_options = (
                    {"frozen_target": frozen_target} if frozen_target is not None else {}
                )
                result = refresh_sources(
                    day,
                    store,
                    version.version_id,
                    google,
                    mode="publish",
                    polar_token=os.getenv("POLAR_API_KEY", ""),
                    target_query=os.getenv("TARGET_GMAIL_QUERY", ""),
                    target_workbook_id=os.getenv("TARGET_WORKBOOK_ID", ""),
                    **source_options,
                )
                if result.get("status") != "verified":
                    raise ValueError("Tracker sources are incomplete")
            store.finish_run(run_id, "verified", version_id=version.version_id)
            return {"status": "verified", "day": day.isoformat(), "version_id": version.version_id}
        except Exception:
            store.finish_run(run_id, "failed", error_code=f"{command}_failed")
            raise RuntimeError("pipeline_failed") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verified sales cloud pipeline")
    parser.add_argument(
        "command",
        choices=(
            "sales",
            "warehouse",
            "trackers",
            "operations",
            "monitor",
            "operations-monitor",
            "compare",
            "migrate",
        ),
    )
    parser.add_argument("--day", type=date.fromisoformat)
    parser.add_argument("--version", dest="version_id")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--at-local-time", metavar="HH:MM")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    try:
        if args.at_local_time and not local_time_matches(now, args.at_local_time):
            print(json.dumps({"status": "skipped", "reason": "outside_local_schedule"}))
            return 0
        if args.command in {"sales", "warehouse", "trackers", "operations", "migrate"}:
            require_writes()
        with Store(os.environ["DATABASE_URL"]) as store:
            if args.command == "migrate":
                if args.day or args.version_id or args.fresh:
                    raise ValueError("Migration does not accept sales options")
                store.migrate()
                print(json.dumps({"status": "verified", "command": "migrate"}))
                return 0
            google = GoogleAPI(
                os.environ["GOOGLE_CLIENT_ID"],
                os.environ["GOOGLE_CLIENT_SECRET"],
                os.environ["GOOGLE_REFRESH_TOKEN"],
            )
            slack = SlackReport(
                os.environ["SLACK_BOT_TOKEN"], os.environ["SLACK_REPORT_CHANNEL_ID"]
            )
            readbacks = None
            if args.command in {"monitor", "operations-monitor", "compare"}:
                from .health_readbacks import native_readbacks

                readbacks = native_readbacks(store, google, slack)
            frozen_target = None
            if args.command == "operations":
                from based_inventory.velocity_archive import VelocityArchive

                from .platform_trackers import check_or_publish, report_inputs

                reporting_day = args.day or expected_reporting_day(now)
                if args.fresh:
                    raise ValueError("Operations cannot collect fresh warehouse sales")
                if args.version_id:
                    selected = store.load(args.version_id)
                    if selected.day != reporting_day:
                        raise ValueError("Saved version does not match reporting day")
                    store.assert_current(args.version_id)
                with VelocityArchive(os.environ["DATABASE_URL"], read_only=True) as storage:
                    _, frozen_target = report_inputs(storage, reporting_day, slack)
                # These six views consume the complete delivered native package,
                # not the warehouse pull. A warehouse failure must not strand them.
                report_version = check_or_publish(
                    os.environ["DATABASE_URL"], reporting_day, google, slack, publish=True
                )
            result = run_command(
                {"operations": "trackers", "operations-monitor": "monitor"}.get(
                    args.command, args.command
                ),
                store=store,
                google=google,
                slack=slack,
                day=args.day,
                version_id=args.version_id,
                fresh=args.fresh,
                now_utc=now,
                provider_readbacks=readbacks,
                native_report=args.command == "operations-monitor",
                frozen_target=frozen_target,
            )
            if args.command == "operations":
                result["report_version_id"] = report_version
            elif args.command == "operations-monitor":
                from .platform_trackers import check_or_publish

                report_version = check_or_publish(
                    os.environ["DATABASE_URL"],
                    date.fromisoformat(result["day"]),
                    google,
                    slack,
                    publish=False,
                )
                result["report_version_id"] = report_version
            print(json.dumps(result, sort_keys=True))
            return 0 if result["status"] in {"verified", "healthy"} else 1
    except Exception:
        # Provider exception bodies and configuration values never enter cron logs.
        print(json.dumps({"status": "failed", "error_code": "pipeline_command_failed"}))
        return 1
