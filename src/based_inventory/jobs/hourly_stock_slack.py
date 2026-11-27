"""Keep the hourly stock posts together in one Slack thread per Pacific date."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from based_inventory.pipeline.store import LOCK_ID
from based_inventory.slack import SlackClient

PACIFIC = ZoneInfo("America/Los_Angeles")
MIGRATION = Path(__file__).resolve().parents[3] / "migrations/003_hourly_stock_slack.sql"


def publish_hourly_status(
    database_url: str, slack: SlackClient, status: str, *, checked_at: datetime
) -> bool:
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise ValueError("Hourly Slack delivery requires a PostgreSQL connection")
    if checked_at.utcoffset() is None:
        raise ValueError("Hourly Slack delivery requires a timezone-aware timestamp")
    local = checked_at.astimezone(PACIFIC)
    hour = checked_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    slot = (slack.channel, hour)

    try:
        with psycopg.connect(
            database_url,
            autocommit=True,
            connect_timeout=10,
            options="-c statement_timeout=10000 -c lock_timeout=5000",
        ) as db:
            # The sales pipeline also applies the migrations in this directory.
            with db.transaction():
                db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID + 1,))
                db.execute(MIGRATION.read_text(encoding="utf-8"))

            existing = db.execute(
                "SELECT slack_ts FROM hourly_stock_slack_posts WHERE channel=%s AND check_hour=%s",
                slot,
            ).fetchone()
            if existing:
                if existing[0] is None:
                    raise RuntimeError("Slack delivery for this hour is unconfirmed")
                return True

            parent = db.execute(
                "SELECT slack_ts FROM hourly_stock_slack_posts "
                "WHERE channel=%s AND pacific_day=%s AND is_parent",
                (slack.channel, local.date()),
            ).fetchone()
            if parent is not None and parent[0] is None:
                raise RuntimeError("The day's Slack post is unconfirmed")

            # Commit the slot before calling Slack. A crash or timeout cannot
            # trigger a duplicate parent or a second send for the same hour.
            reserved = db.execute(
                "INSERT INTO hourly_stock_slack_posts (channel, check_hour, pacific_day, is_parent) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING check_hour",
                (*slot, local.date(), parent is None),
            ).fetchone()
            if reserved is None:
                raise RuntimeError("Another run reserved this hour or the day's Slack post")

            if parent is None:
                day_label = f"{local:%B} {local.day}, {local:%Y}"
                details = status.partition("\n")[2] or status
                status = (
                    f"ShipHero stock tracker for {day_label} (Pacific time).\n"
                    "This is today's first posted stock check for the Merchdrop warehouse. "
                    "Later checks will be posted in this thread.\n"
                    f"{details}"
                )
            lines = status.splitlines()
            blocks = [
                {
                    "type": "section",
                    "text": {"type": "plain_text", "text": "\n".join(lines[i : i + 12])},
                }
                for i in range(0, len(lines), 12)
            ]
            timestamp = slack.post_with_receipt(
                status, blocks, thread_ts=parent[0] if parent else None
            )
            if timestamp is None:
                db.execute(
                    "DELETE FROM hourly_stock_slack_posts "
                    "WHERE channel=%s AND check_hour=%s AND slack_ts IS NULL",
                    slot,
                )
                return False
            saved = db.execute(
                "UPDATE hourly_stock_slack_posts SET slack_ts=%s "
                "WHERE channel=%s AND check_hour=%s AND slack_ts IS NULL RETURNING slack_ts",
                (timestamp, *slot),
            ).fetchone()
            if saved is None:
                raise RuntimeError(
                    "Slack posted the stock update but its receipt could not be saved"
                )
            return True
    except psycopg.Error:
        raise RuntimeError("The hourly stock Slack receipt database was unavailable") from None
