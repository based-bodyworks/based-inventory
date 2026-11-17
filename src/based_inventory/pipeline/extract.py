"""Run the existing fresh extractor and cross-check its three saved outputs."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import openpyxl

from based_inventory.shiphero import MERCHDROP_WAREHOUSE_ID

from .models import Coverage, SalesRow, VerifiedDay

CHANNEL_LABELS = {"SHOPIFY": "Shopify", "TIKTOK": "TIKTOK", "AMAZON (FBM)": "Amazon FBM"}
SHOP_LABELS = {
    "basedbodyworks.myshopify.com": "Shopify",
    "BASED": "TIKTOK",
    "Based Bodyworks Amazon": "Amazon FBM",
}
ROOT = Path(__file__).resolve().parents[3]


def artifact(stem: Path, extension: str) -> Path:
    return stem.with_suffix("." + extension)


def load_extract(stem: Path, day: date, commit: str, captured_at: datetime) -> VerifiedDay:
    state = json.loads(artifact(stem, "checkpoint.json").read_text())
    key = day.isoformat()
    if (
        state.get("schema") != 7
        or state.get("definition") != "ordered"
        or state.get("warehouse_id") != MERCHDROP_WAREHOUSE_ID
        or set(state.get("days", {})) != {key}
    ):
        raise ValueError("Expected a fresh single-day schema-7 extraction")
    proof = state.get("source_coverage", {}).get(key)
    if not proof:
        raise ValueError("Missing fresh order/line coverage receipt")
    coverage = Coverage(**proof)
    coverage.validate()
    if state.get("source_order_counts", {}).get(key) != coverage.fetched_unique:
        raise ValueError("Source order count differs from coverage")
    expected = {}
    for shop, skus in state["days"][key].items():
        if shop not in SHOP_LABELS:
            raise ValueError("Unexpected merchant sales channel")
        for sku, units in skus.items():
            expected[(sku, SHOP_LABELS[shop])] = units
    rows = []
    with artifact(stem, "csv").open(newline="") as file:
        for row in csv.DictReader(file):
            if row["Date"] != key or row["Channel"] not in CHANNEL_LABELS:
                raise ValueError("Unexpected CSV day or channel")
            rows.append(
                SalesRow(
                    row["SKU CODE"],
                    row["SKU"],
                    CHANNEL_LABELS[row["Channel"]],
                    int(row["QTY SOLD"]),
                )
            )
    actual = {(r.sku, r.channel): r.units for r in rows}
    if any(actual.get(k, 0) != v for k, v in expected.items()) or any(
        expected.get(k, 0) != v for k, v in actual.items()
    ):
        raise ValueError("CSV differs from verified extraction checkpoint")
    workbook = openpyxl.load_workbook(artifact(stem, "xlsx"), data_only=True)
    workbook_rows = []
    for sheet in workbook:
        if sheet.title.startswith("DAILY VELOCITY - "):
            label = sheet.title.removeprefix("DAILY VELOCITY - ")
            if label not in CHANNEL_LABELS:
                raise ValueError("Unexpected workbook channel")
            for d, name, units, sku in list(sheet.values)[1:]:
                if d != key:
                    raise ValueError("Unexpected workbook date")
                workbook_rows.append(SalesRow(str(sku), name, CHANNEL_LABELS[label], units))
    if sorted(workbook_rows, key=lambda r: (r.sku, r.channel)) != sorted(
        rows, key=lambda r: (r.sku, r.channel)
    ):
        raise ValueError("Saved workbook differs from verified CSV")
    return VerifiedDay(
        day,
        tuple(rows),
        coverage,
        state.get("excluded_shops", {}),
        state["catalog_fingerprint"],
        commit,
        artifact(stem, "xlsx").read_bytes(),
        captured_at,
    )


def extract_day(day: date, *, heartbeat=None) -> VerifiedDay:
    if day >= datetime.now(UTC).date():
        raise ValueError("Extraction requires a complete UTC day")
    commit = (
        os.getenv("RENDER_GIT_COMMIT")
        or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    )
    with TemporaryDirectory(prefix="verified-sales-") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts/daily_sales_velocity.py"),
                "--start-date",
                day.isoformat(),
                "--end-date",
                day.isoformat(),
                "--out-dir",
                output,
                "--fresh",
            ],
            cwd=ROOT,
        )
        started = time.monotonic()
        try:
            while True:
                try:
                    code = process.wait(timeout=30)
                    if code:
                        raise subprocess.CalledProcessError(code, process.args)
                    break
                except subprocess.TimeoutExpired:
                    if heartbeat:
                        heartbeat()
                    if time.monotonic() - started > 10800:
                        raise TimeoutError("Merchant extraction exceeded three hours") from None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        stem = Path(output) / f"daily-sales-velocity_{day}_{day}"
        return load_extract(stem, day, commit, datetime.now(UTC))
