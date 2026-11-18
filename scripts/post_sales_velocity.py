#!/usr/bin/env python3
"""Daily all-channel SKU sales, with independent warehouse context."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from psycopg import Error as DatabaseError  # noqa: E402

from based_inventory.all_channel_report import (  # noqa: E402
    build_sales_workbook,
    render_sales_report,
)
from based_inventory.all_channel_sales import SalesDay, collect_sales_day  # noqa: E402
from based_inventory.pipeline.amazon_refresh import PolarAPI  # noqa: E402
from based_inventory.pipeline.google_api import GoogleAPI  # noqa: E402
from based_inventory.pipeline.runner import local_time_matches  # noqa: E402
from based_inventory.pipeline.slack_report import SlackReport, SlackReportError  # noqa: E402
from based_inventory.pipeline.store import Store  # noqa: E402
from based_inventory.pipeline.target_refresh import SourceIncomplete  # noqa: E402
from based_inventory.sales_report_archive import (  # noqa: E402
    SalesReportArchive,
    SalesReportPackage,
)
from based_inventory.shiphero import ShipHeroClient  # noqa: E402
from based_inventory.shiphero_auth import resolve_access_token  # noqa: E402
from based_inventory.slack import SlackClient, section  # noqa: E402
from based_inventory.velocity_archive import POSTGRES_SCHEMES, VelocityArchive  # noqa: E402

TARGET_QUERY = 'from:zach.haugen@onepltfrm.com subject:"Target Daily Sales Reporting"'


def _target_query(day):
    base = os.getenv("TARGET_GMAIL_QUERY", TARGET_QUERY).strip()
    start = (day - timedelta(days=3)).strftime("%Y/%m/%d")
    end = (day + timedelta(days=3)).strftime("%Y/%m/%d")
    return f"{base} after:{start} before:{end}"


def _required(name):
    value = os.getenv(name)
    if not value:
        raise SourceIncomplete(f"Required server credential {name} is not configured")
    return value


def _clients():
    polar = PolarAPI(_required("POLAR_API_KEY"))
    google = GoogleAPI(
        _required("GOOGLE_CLIENT_ID"),
        _required("GOOGLE_CLIENT_SECRET"),
        _required("GOOGLE_REFRESH_TOKEN"),
    )
    token = resolve_access_token(
        refresh_token=os.getenv("SHIPHERO_REFRESH_TOKEN", ""),
        fallback_access_token=os.getenv("SHIPHERO_ACCESS_TOKEN", ""),
    )
    shiphero = ShipHeroClient(
        token=token,
        api_url=os.getenv("SHIPHERO_API_URL", "https://public-api.shiphero.com/graphql"),
    )
    return polar, shiphero, google


def _merchant_context(location, day):
    if not location.startswith(POSTGRES_SCHEMES):
        return None
    try:
        with Store(location) as store:
            return store.latest(day)
    except (DatabaseError, ValueError):
        # This optional context is never a substitute for native platform sales.
        print("Warehouse-only context is unavailable; no warehouse units were added.")
        return None


def _pending_notice(storage, day, token, channel, reason):
    key = f"sales-pending:{day}:{channel}"
    if storage._read(key) is not None:
        return
    names = [
        name
        for name in ("Shopify", "TikTok", "Amazon", "Target")
        if name.lower() in str(reason).lower()
    ]
    source = ", ".join(names) if names else "Required source"
    text = (
        f"*Daily SKU Velocity | {day}*\n"
        f"Report pending: {source} data has not passed the checks for this reporting date. "
        "The all-channel report has been withheld. Missing data will not appear as zero sales."
    )
    # Reserve the notice attempt first. An uncertain response must not create a
    # stream of repeated notices; the complete report uses a separate receipt.
    storage._write(key, {"attempted": True, "source": source}, once=True)
    if not SlackClient(token, channel).post_message(text, [section(text)]):
        raise RuntimeError("The pending-report notice could not be verified")


def _output(package, directory):
    if directory:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"daily-sales_{package.day}.md").write_text(package.summary + "\n")
        (directory / f"daily-sales_{package.day}.xlsx").write_bytes(package.report)


def _package(current, history, merchant=None):
    history = {
        day: record for day, record in history.items() if record.timezone == current.timezone
    }
    history[current.day] = current
    return SalesReportPackage(
        day=current.day,
        capture_id=current.capture_id,
        history_ids={day.isoformat(): record.capture_id for day, record in history.items()},
        summary=render_sales_report(current, history, merchant),
        report=build_sales_workbook(current, history, merchant),
        merchant=merchant,
    )


def _run(args, now):
    load_dotenv(ROOT / ".env")
    preview = args.preview or os.getenv("DRY_RUN", "0") == "1"
    source = (
        SalesDay.from_payload(json.loads(Path(args.sources).read_text())) if args.sources else None
    )
    day = (
        date.fromisoformat(args.day)
        if args.day
        else (
            source.day
            if source is not None
            else now.astimezone(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1)
        )
    )
    if source is not None and source.day != day:
        raise ValueError("Local sales capture has a different reporting date")
    if preview:
        if source is None:
            raise ValueError(
                "Preview requires a checked local --sources capture; it never collects live data"
            )
        history = {}
        if args.history:
            if "://" in args.history:
                raise ValueError("Preview accepts only explicit local history")
            with VelocityArchive(args.history) as storage:
                history = SalesReportArchive(storage).window(day)
        package = _package(source, history)
        _output(package, args.out_dir)
        print(package.summary)
        return 0

    location = (
        args.history
        or os.getenv("VELOCITY_DATABASE_URL")
        or str(ROOT / "data/sales-velocity/all-channel-history")
    )
    if os.getenv("ENV") == "prod" and not location.startswith(POSTGRES_SCHEMES):
        raise ValueError(
            "Production all-channel sales require persistent PostgreSQL via VELOCITY_DATABASE_URL"
        )
    token = os.getenv("SLACK_BOT_TOKEN")
    if not args.archive_only and not token:
        raise ValueError("SLACK_BOT_TOKEN is required; use --preview for an offline draft")
    channel = args.channel or os.getenv("SLACK_CHANNEL", "C0AK6UGA1NJ")
    with VelocityArchive(location) as storage, storage.lock(day):
        archive = SalesReportArchive(storage)
        package = archive.latest_package(day)
        if package is None:
            current = archive.latest_day(day)
            if current is None:
                try:
                    if source is None:
                        polar, shiphero, google = _clients()
                        source = collect_sales_day(day, polar, shiphero, google, _target_query(day))
                    current = archive.save_day(source)
                except SourceIncomplete as error:
                    if not args.archive_only:
                        _pending_notice(storage, day, token, channel, error)
                    raise
            elif source is not None and current.capture_id != source.capture_id:
                raise ValueError("Different same-date sources require reconciliation")
            if args.archive_only:
                print(f"Saved complete all-channel source capture for {day}; no report sent")
                return 0
            package = archive.save_package(
                _package(current, archive.window(day), _merchant_context(location, day))
            )
        elif source is not None and package.capture_id != source.capture_id:
            raise ValueError("A different report already belongs to this date")
        if args.archive_only:
            print(f"Complete report already saved for {day}; no publication requested")
            return 0
        _output(package, args.out_dir)
        file_id = SlackReport(token, channel).publish(
            archive.delivery_store(package, channel),
            package,
            summary_text=package.summary,
            report_title=f"BASED Daily SKU Sales Velocity | {package.day}",
        )
    print(f"All-channel daily SKU report checked in {channel} for {day} ({file_id})")
    return 0


def main(argv=None, *, now_utc=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", help="Sales reporting date, YYYY-MM-DD")
    parser.add_argument(
        "--at-local-time", help="Only start within one hour of this Pacific time, HH:MM"
    )
    parser.add_argument("--channel")
    parser.add_argument("--history", help="Dedicated local archive or persistent PostgreSQL URL")
    parser.add_argument("--sources", help="Previously checked complete sales capture JSON")
    parser.add_argument("--out-dir", help="Save the exact message and workbook locally")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--preview", action="store_true", help="Render local captures without network access"
    )
    mode.add_argument(
        "--archive-only", action="store_true", help="Save checked source history without sending"
    )
    args = parser.parse_args(argv)
    now = now_utc or datetime.now(UTC)
    try:
        if args.at_local_time and not local_time_matches(now, args.at_local_time):
            print("Skipped the adjacent daylight-saving schedule slot")
            return 0
        return _run(args, now)
    except DatabaseError:
        print("ERROR: persistent sales history is unavailable", file=sys.stderr)
    except (SourceIncomplete, SlackReportError, ValueError, RuntimeError) as error:
        print(f"ERROR: daily all-channel sales report unavailable: {error}", file=sys.stderr)
    except OSError:
        print("ERROR: required local report files are unavailable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
