"""Tests for the Slack summary built by post_velocity_to_slack.

Covers the CSV -> summary text path: totals, channel ordering, top SKUs, the
bundle-methodology footer, and the optional as-sold Top-bundles line read from
the sibling .bundles.csv.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import post_velocity_to_slack as pvs  # noqa: E402


def _write_main_csv(path: Path) -> None:
    rows = [
        ["Date", "SKU", "Channel", "QTY SOLD", "SKU CODE"],
        ["2026-06-30", "Texture Powder", "TIKTOK", "5", "TP1"],
        ["2026-06-30", "Texture Powder", "SHOPIFY", "3", "TP1"],
        ["2026-06-30", "Clay", "SHOPIFY", "2", "CLAY1"],
    ]
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def _write_bundles_csv(path: Path) -> None:
    rows = [
        ["Date", "Bundle", "Channel", "QTY SOLD", "SKU CODE"],
        ["2026-06-30", "Curly Kit", "TIKTOK", "3", "KIT1"],
        ["2026-06-30", "Curly Kit", "SHOPIFY", "1", "KIT1"],
        ["2026-06-30", "Shower Duo", "SHOPIFY", "0", "DUO1"],
        ["2026-06-29", "Curly Kit", "TIKTOK", "99", "KIT1"],  # other day, must not count
    ]
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def test_summary_totals_channels_and_bundle_methodology_footer(tmp_path) -> None:
    p = tmp_path / "daily-velocity_2026-06-30.csv"
    _write_main_csv(p)
    out = pvs._summary_from_csv(p, "2026-06-30")
    assert "Total: *10*" in out
    assert "TIKTOK 5" in out and "SHOPIFY 5" in out
    # The methodology footer must say bundles are counted inside components.
    assert "component SKUs" in out


def test_summary_appends_top_bundles_when_bundles_csv_present(tmp_path) -> None:
    p = tmp_path / "daily-velocity_2026-06-30.csv"
    b = tmp_path / "daily-velocity_2026-06-30.bundles.csv"
    _write_main_csv(p)
    _write_bundles_csv(b)
    out = pvs._summary_from_csv(p, "2026-06-30", bundles_csv=b)
    assert "Top bundles" in out
    # 3 TikTok + 1 Shopify on the day; the 99-unit 6/29 row is filtered out.
    assert "Curly Kit 4" in out
    assert "Shower Duo" not in out  # zero-unit bundles are dropped from the line


def test_summary_skips_bundles_line_when_file_missing(tmp_path) -> None:
    p = tmp_path / "daily-velocity_2026-06-30.csv"
    _write_main_csv(p)
    out = pvs._summary_from_csv(p, "2026-06-30", bundles_csv=tmp_path / "nope.bundles.csv")
    assert "Top bundles" not in out


def test_malformed_bundles_csv_never_blocks_the_main_post(tmp_path) -> None:
    # The bundles line is decorative; a broken .bundles.csv must not take down
    # the required daily post (the workbook upload happens after this returns).
    p = tmp_path / "daily-velocity_2026-06-30.csv"
    b = tmp_path / "daily-velocity_2026-06-30.bundles.csv"
    _write_main_csv(p)
    b.write_text("Wrong,Header\noops,not-a-number\n")
    out = pvs._summary_from_csv(p, "2026-06-30", bundles_csv=b)
    assert "Total: *10*" in out  # main summary intact
    assert "Top bundles" not in out  # bad file silently skipped
