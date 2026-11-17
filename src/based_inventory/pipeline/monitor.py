"""Read-only health checks independent of the jobs they observe.

Native readers must compare the current provider values, version and report hash
against the supplied immutable version. A receipt alone is never proof of health.
No notification is sent here; alert templates require approval before activation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from .models import VerifiedDay, canonical, digest

PACIFIC = "America/Los_Angeles"
MERCHANT_DESTINATIONS = (
    "report_archive",
    "history",
    "canonical",
    "rolling",
    "erp",
    "inventory",
    "slack",
)
SOURCE_DESTINATIONS = ("target", "amazon", "workbook")
HEARTBEAT_MAX_AGE = timedelta(minutes=20)
MAX_RUN_AGE = timedelta(hours=3)
DEADLINE = time(8, 45)


@dataclass(frozen=True)
class HealthReport:
    state: Literal["healthy", "running", "failed", "incomplete", "stale"]
    reason_codes: tuple[str, ...]
    version_id: str | None
    expected_day: date
    message: str


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware clock is required")
    return value.astimezone(UTC)


def expected_reporting_day(now_utc: datetime, timezone: str = PACIFIC) -> date:
    """Return yesterday in the schedule's local calendar, including DST days."""
    return _utc(now_utc).astimezone(ZoneInfo(timezone)).date() - timedelta(days=1)


def _result(state, reasons, version, day):
    descriptions = {
        "healthy": "Sales, trackers and the matching report passed saved and live checks.",
        "running": "A job is still running within its allowed time.",
        "failed": "A required job failed or did not start by the morning deadline.",
        "incomplete": "Some required results could not be verified.",
        "stale": "A job stopped updating or current results do not match the saved sales version.",
    }
    codes = tuple(sorted(set(reasons)))
    message = f"Sales and tracker check for {day.isoformat()}: {state}. {descriptions[state]}"
    if codes:
        message += " Checks: " + ", ".join(codes) + "."
    return HealthReport(state, codes, version.version_id if version else None, day, message)


def check_pipeline(
    day: date,
    store,
    provider_readbacks: Callable[[VerifiedDay], dict] | None,
    *,
    now_utc: datetime,
    heartbeat_max_age: timedelta = HEARTBEAT_MAX_AGE,
    max_run_age: timedelta = MAX_RUN_AGE,
) -> HealthReport:
    """Check one reporting day without starting or repairing its jobs.

    Provider callbacks return explicit booleans for each merchant destination.
    Separate source coverage is established by a matching verified tracker run and
    verified target/amazon/workbook receipts. Exceptions from native reads become
    a visible, redacted failure; database failures propagate to the caller.
    """
    now = _utc(now_utc)
    if type(day) is not date or day >= now.date():
        raise ValueError("A complete reporting day is required")
    if heartbeat_max_age <= timedelta(0) or max_run_age <= timedelta(0):
        raise ValueError("Running time limits must be positive")
    local = now.astimezone(ZoneInfo(PACIFIC))
    deadline = datetime.combine(day + timedelta(days=1), DEADLINE, ZoneInfo(PACIFIC))
    overdue = local >= deadline
    version = store.latest(day)
    failures, stale, incomplete, running = [], [], [], []
    for kind in ("sales", "trackers"):
        run = store.latest_run(kind, day)
        if not run:
            (failures if overdue else incomplete).append(f"{kind}_not_started")
            continue
        status = run.get("status")
        if status == "failed":
            failures.append(f"{kind}_failed")
        elif status == "running":
            try:
                age = now - _utc(run["started_at"])
                heartbeat_age = now - _utc(run["updated_at"])
                active = (
                    timedelta(0) <= age <= max_run_age
                    and timedelta(0) <= heartbeat_age <= heartbeat_max_age
                )
            except (KeyError, TypeError, ValueError):
                active = False
            (running if active else stale).append(
                f"{kind}_running" if active else f"{kind}_heartbeat_stale"
            )
        elif status != "verified":
            incomplete.append(f"{kind}_status_unverified")
        elif version and run.get("version_id") != version.version_id:
            stale.append(f"{kind}_version_mismatch")
    if version is None:
        incomplete.append("sales_version_missing")
    else:
        if version.day != day:
            stale.append("sales_day_mismatch")
        receipts = store.receipts(version.version_id)
        for destination in MERCHANT_DESTINATIONS + SOURCE_DESTINATIONS:
            receipt = receipts.get(destination)
            if not receipt:
                incomplete.append(f"{destination}_receipt_missing")
            elif receipt.get("status") != "verified":
                incomplete.append(f"{destination}_receipt_unverified")
            elif destination in ("report_archive", "slack") and receipt.get("digest") != digest(
                version.report
            ):
                incomplete.append(f"{destination}_report_hash_mismatch")
        if provider_readbacks is None:
            incomplete.append("provider_readback_missing")
        else:
            try:
                readbacks = provider_readbacks(version)
                if not isinstance(readbacks, dict):
                    raise ValueError("Invalid native readback")
            except Exception:
                # Never expose provider response text or credentials in bot alerts.
                incomplete.append("provider_readback_failed")
            else:
                for destination in MERCHANT_DESTINATIONS + SOURCE_DESTINATIONS:
                    if readbacks.get(destination) is not True:
                        stale.append(f"{destination}_readback_mismatch")
    reasons = failures + stale + incomplete + running
    state = (
        "failed"
        if failures
        else "stale"
        if stale
        else "running"
        if running
        else "incomplete"
        if incomplete
        else "healthy"
    )
    return _result(state, reasons, version, day)


def alert_identity(report: HealthReport) -> str | None:
    """Stable key for a caller's durable notification reservation, never a send.

    Reserve before sending. An ambiguous delivery must remain reserved until the
    caller reconciles it with the provider; the identity does not promise exactly
    once delivery and is not itself a deduplication database.
    """
    if report.state == "healthy":
        return None
    return digest(
        canonical(
            {
                "day": report.expected_day.isoformat(),
                "state": report.state,
                "reasons": sorted(report.reason_codes),
                "version_id": report.version_id,
            }
        )
    )


def format_alert(report: HealthReport) -> str:
    """Proposed plain-text bot template. No raw exception or user-controlled text."""
    states = {"healthy", "running", "failed", "incomplete", "stale"}
    if report.state not in states:
        raise ValueError("Unknown monitor state")
    # Do not interpolate report.message or arbitrary provider/error text into Slack.
    next_steps = {
        "healthy": "No action is needed.",
        "running": "The next check will confirm completion.",
        "failed": "Check the cloud run record before retrying the saved reporting day.",
        "incomplete": "Check the missing receipts and live readbacks before retrying publication.",
        "stale": "Check the latest sales version and job heartbeat before retrying publication.",
    }
    return f"Sales and tracker check for {report.expected_day.isoformat()}: {report.state}. {next_steps[report.state]}"
