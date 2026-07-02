#!/usr/bin/env python3
"""Post a daily velocity workbook to Slack with a short summary comment.

Reads the sibling long-format CSV (Date, SKU, Channel, QTY SOLD, SKU CODE) to
build the summary, then uploads the .xlsx to the channel. Used by the daily
Render cron (based-inventory-velocity; see render.yaml) after the pull completes.

Usage:
    post_velocity_to_slack.py --xlsx <path> --day YYYY-MM-DD --channel <id>
Reads SLACK_BOT_TOKEN from .env. Exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from based_inventory.slack import SlackClient, context, section  # noqa: E402

# Channel-label display order for the summary line.
_ORDER = ["TIKTOK", "SHOPIFY", "AMAZON (FBM)"]


def _top_bundles_line(bundles_csv: Path, day: str, limit: int = 5) -> str | None:
    """AS-SOLD top-bundles line for one day from the sibling .bundles.csv, or None.

    These units are already inside the exploded totals above it -- the line is
    visibility (which packs/kits sold), never something to add to the total.
    Decorative: any problem reading the file returns None rather than letting
    a broken optional line take down the required daily post.
    """
    if not bundles_csv.exists():
        return None
    by_bundle: dict[str, int] = defaultdict(int)
    try:
        with open(bundles_csv) as f:
            for row in csv.DictReader(f):
                if row["Date"] != day:
                    continue
                by_bundle[row["Bundle"]] += int(row["QTY SOLD"] or 0)
    except (KeyError, ValueError, OSError, csv.Error):
        return None
    top = [(n, q) for n, q in sorted(by_bundle.items(), key=lambda kv: -kv[1]) if q > 0][:limit]
    if not top:
        return None
    top_str = " · ".join(f"{name} {q:,}" for name, q in top)
    return f"Top bundles (as-sold, already in the totals): {top_str}"


def _summary_from_csv(csv_path: Path, day: str, bundles_csv: Path | None = None) -> str:
    by_channel: dict[str, int] = defaultdict(int)
    by_sku: dict[str, int] = defaultdict(int)
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            qty = int(row["QTY SOLD"] or 0)
            by_channel[row["Channel"]] += qty
            by_sku[row["SKU"]] += qty

    total = sum(by_channel.values())
    channels = [c for c in _ORDER if c in by_channel] + sorted(
        c for c in by_channel if c not in _ORDER
    )
    chan_str = " · ".join(f"{c} {by_channel[c]:,}" for c in channels)
    top = sorted(by_sku.items(), key=lambda kv: -kv[1])[:5]
    top_str = " · ".join(f"{name} {q:,}" for name, q in top)

    lines = [
        f"*Daily sales velocity — {day}* (units ordered, demand; UTC day)",
        f"Total: *{total:,}* units   ·   {chan_str}",
        f"Top SKUs: {top_str}",
    ]
    bundles_line = _top_bundles_line(bundles_csv, day) if bundles_csv else None
    if bundles_line:
        lines.append(bundles_line)
    lines.append(
        "_Multipacks/bundles are counted inside their component SKUs "
        "(BUNDLE SALES tab lists them as-sold). Amazon = FBM only (FBA not in "
        "ShipHero). Full per-day x SKU x channel grid in the file._"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Post a daily velocity workbook to Slack.")
    p.add_argument("--xlsx", required=True)
    p.add_argument("--day", required=True)
    p.add_argument("--channel", required=True)
    args = p.parse_args(argv)

    load_dotenv(ROOT / ".env")
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN not set in .env", file=sys.stderr)
        return 1

    xlsx = Path(args.xlsx)
    if not xlsx.exists():
        print(f"ERROR: xlsx not found: {xlsx}", file=sys.stderr)
        return 1
    csv_path = xlsx.with_suffix(".csv")
    bundles_csv = xlsx.with_name(xlsx.stem + ".bundles.csv")
    comment = (
        _summary_from_csv(csv_path, args.day, bundles_csv=bundles_csv)
        if csv_path.exists()
        else f"*Daily sales velocity — {args.day}* (units ordered, demand; UTC day)"
    )

    client = SlackClient(token=token, channel=args.channel)

    # Preferred: attach the workbook (needs the files:write scope).
    if client.upload_file(
        str(xlsx),
        title=f"Daily Sales Velocity {args.day}",
        initial_comment=comment,
        channel=args.channel,
    ):
        print(f"posted workbook {xlsx.name} -> {args.channel}")
        return 0

    # Fallback: file upload unavailable (e.g. missing files:write scope) -- post
    # the summary as a message so the channel still gets the daily numbers. Add
    # files:write to the Slack app to start attaching the actual workbook.
    blocks = [
        section(comment),
        context(
            "Add the *files:write* scope to the Slack app to attach the full workbook. "
            f"Saved locally: `{xlsx}`"
        ),
    ]
    if client.post_message(fallback_text=f"Daily sales velocity {args.day}", blocks=blocks):
        print(f"posted summary message -> {args.channel} (no file; add files:write to attach it)")
        return 0

    print("ERROR: Slack post failed (see log)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
