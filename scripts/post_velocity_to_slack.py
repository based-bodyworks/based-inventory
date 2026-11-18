#!/usr/bin/env python3
"""Publish all-SKU daily velocity from one complete, cross-checked UTC day.

The sibling CSV, workbook and schema-7 checkpoint must agree. Verified days
are kept in a separate durable archive for the seven-day rate. Preview never
uses credentials or the network; historical imports require the same proof.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from redis.exceptions import RedisError  # noqa: E402

from based_inventory.daily_velocity_report import render_report  # noqa: E402
from based_inventory.pipeline.extract import load_extract  # noqa: E402
from based_inventory.pipeline.slack_report import SlackReport, SlackReportError  # noqa: E402
from based_inventory.velocity_archive import VelocityArchive  # noqa: E402


def _load_version(xlsx: Path, day: date):
    if xlsx.suffix != ".xlsx":
        raise ValueError("Expected the saved daily .xlsx report")
    commit = (
        os.getenv("RENDER_GIT_COMMIT")
        or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    )
    return load_extract(xlsx.with_suffix(""), day, commit, datetime.now(UTC))


def _run(args) -> int:
    load_dotenv(ROOT / ".env")
    day = date.fromisoformat(args.day)
    version = _load_version(Path(args.xlsx), day)
    preview = args.preview or os.getenv("DRY_RUN", "0") == "1"
    if preview:
        # Environment state may be a production Redis URL. A preview must not
        # contact it, nor create any history/receipt merely to print a draft.
        history = {}
        if args.history:
            if args.history.startswith(("redis://", "rediss://")):
                raise ValueError(
                    "Preview accepts only explicit local history, never remote history"
                )
            with VelocityArchive(args.history) as archive:
                history = archive.window(day)
        print(render_report(version, history))
        return 0

    location = (
        args.history
        or os.getenv("VELOCITY_HISTORY_PATH")
        or os.getenv("STATE_PATH")
        or str(ROOT / "data/sales-velocity/history")
    )
    if os.getenv("ENV") == "prod" and not location.startswith(("redis://", "rediss://")):
        raise ValueError("Production daily velocity requires a durable Redis history archive")
    token = os.getenv("SLACK_BOT_TOKEN")
    if not args.archive_only and not token:
        raise ValueError("SLACK_BOT_TOKEN is required to publish; use --preview for a draft")
    channel = args.channel or os.getenv("SLACK_CHANNEL", "C0AK6UGA1NJ")
    with VelocityArchive(location) as archive, archive.lock(day):
        version = archive.save(version)
        if args.archive_only:
            print(f"Archived verified daily source for {day}; no Slack message sent")
            return 0
        summary = render_report(version, archive.window(day))
        file_id = SlackReport(token, channel).publish(
            archive.delivery_store(version, channel), version, summary_text=summary
        )
    print(f"Verified daily SKU report for {day} in {channel} ({file_id})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", required=True)
    parser.add_argument("--day", required=True, help="Complete UTC day, YYYY-MM-DD")
    parser.add_argument("--channel", help="Slack channel; defaults to SLACK_CHANNEL")
    parser.add_argument("--history", help="Dedicated local archive directory or Redis URL")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--preview", action="store_true", help="Print the draft without any network access"
    )
    mode.add_argument(
        "--archive-only", action="store_true", help="Import verified source without sending"
    )
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except (ValueError, RuntimeError, SlackReportError) as error:
        print(f"ERROR: daily SKU velocity unavailable: {error}", file=sys.stderr)
    except OSError:
        print(
            "ERROR: required daily source artifacts or archive files are unavailable",
            file=sys.stderr,
        )
    except RedisError:
        print("ERROR: durable daily velocity archive is unavailable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
