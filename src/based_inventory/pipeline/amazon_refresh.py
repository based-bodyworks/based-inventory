"""Supported Polar HTTP transport and validated AFN/listing source refresh."""

from __future__ import annotations

import contextlib
import json
import time
from datetime import UTC, date, datetime, timedelta

import requests

from .cloud_helpers.amazon_inventory_plan import DATASOURCE_ID, build_plan, verify_readback
from .cloud_helpers.collect_inventory_snapshot import cell
from .cloud_helpers.normalize_afn import METRIC, normalize_source
from .google_api import DYNAMIC_ID, GoogleAPIError
from .target_refresh import SourceIncomplete, asset, validate_mode, verify_requests

MARKETPLACE = "ATVPDKIKX0DER"


class PolarAPI:
    """Server API key only; never accepts a Codex session or connector token."""

    def __init__(self, token, *, session=None, sleep=time.sleep):
        if not token:
            raise SourceIncomplete("Server Polar API key is not configured")
        self.token = token
        self.session = session or requests.Session()
        self.sleep = sleep
        self.conversation = None

    def call(self, tool, payload):
        if tool not in {
            "get_context",
            "list_dimensions",
            "get_metrics",
            "generate_report",
            "fetch_live_connector_data",
        }:
            raise ValueError("Polar tool is outside the read allowlist")
        for attempt in range(5):
            try:
                response = self.session.post(
                    "https://api.polaranalytics.com/mcp/tool/" + tool,
                    json=payload,
                    headers={"Authorization": "Bearer " + self.token},
                    timeout=60,
                )
            except requests.RequestException:
                raise SourceIncomplete("Polar API request unavailable") from None
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                self.sleep(2**attempt)
                continue
            if response.status_code != 200:
                raise SourceIncomplete(f"Polar API HTTP {response.status_code}")
            try:
                result = response.json()
            except ValueError:
                raise SourceIncomplete("Polar response is not valid JSON") from None
            if result.get("isError") or result.get("error"):
                raise SourceIncomplete("Polar source request was rejected")
            result = result.get("structuredContent", result)
            if result.get("error"):
                raise SourceIncomplete("Polar source request was rejected")
            return result
        raise SourceIncomplete("Polar API retries exhausted")

    def context(self):
        result = self.call(
            "get_context",
            {
                "initialQuestion": "Refresh approved Target-independent Amazon AFN and listing inventory sources",
                "version": "4.8",
            },
        )
        status = result.get("account_status", {})
        if status.get("activation") != "activated" or status.get("data") != "ready":
            raise SourceIncomplete("Polar account data is unavailable")
        self.conversation = result.get("conversation_id")
        if not self.conversation:
            raise SourceIncomplete("Polar conversation context missing")
        return result

    def live(self, operation, params):
        return self.call(
            "fetch_live_connector_data",
            {
                "conversation_id": self.conversation,
                "connector_key": "amazon-selling-partner",
                "datasource_id": DATASOURCE_ID,
                "operation": operation,
                "params": params,
            },
        )


def validate_utc_intervals(result, start, end):
    if result.get("truncated") is not False or result.get("row_count") != len(
        result.get("rows", [])
    ):
        raise SourceIncomplete("Amazon UTC boundary control is incomplete")
    expected = {start + timedelta(days=i) for i in range((end - start).days + 1)}
    actual = set()
    for row in result.get("rows", []):
        if row.get("marketplace_id") != MARKETPLACE:
            raise SourceIncomplete("Amazon boundary control has wrong marketplace")
        first = datetime.fromisoformat(row["interval_start"].replace("Z", "+00:00"))
        last = datetime.fromisoformat(row["interval_end"].replace("Z", "+00:00"))
        if (
            first.utcoffset() != timedelta(0)
            or first.timetz().replace(tzinfo=None) != datetime.min.time()
            or last - first != timedelta(days=1)
        ):
            raise SourceIncomplete("Amazon provider interval does not prove complete UTC dates")
        if first.date() in actual:
            raise SourceIncomplete("Duplicate provider UTC interval")
        actual.add(first.date())
    if actual != expected:
        raise SourceIncomplete("Amazon provider UTC control dates are incomplete")


def collect_amazon(day: date, polar, shiphero, *, start=None):
    start = start or day - timedelta(days=6)
    if day >= datetime.now(UTC).date():
        raise SourceIncomplete("Amazon requires complete UTC dates")
    polar.context()
    dimensions = polar.call(
        "list_dimensions", {"conversation_id": polar.conversation, "metrics": METRIC}
    )
    required = {"fulfillment_channel", "marketplace_id", "sales_channel", "fulfillment_status"}
    if not required <= set(dimensions.get("filters", [])) or "sku" not in dimensions.get(
        "dimensions", []
    ):
        raise SourceIncomplete("Polar AFN dimension contract changed")
    rules = {
        k: [{"operator": op, "value": [v]}]
        for k, op, v in [
            ("fulfillment_channel", "IS", "AFN"),
            ("marketplace_id", "IS", MARKETPLACE),
            ("sales_channel", "IS", "Amazon.com"),
            ("fulfillment_status", "ISNOT", "Canceled"),
        ]
    }
    payload = {
        "conversation_id": polar.conversation,
        "metrics": METRIC,
        "dimensions": ["sku"],
        "dateRangeFrom": start.isoformat(),
        "dateRangeTo": day.isoformat(),
        "granularity": "day",
        "ordering": ["date:ASC"],
        "settings": json.dumps({"timezone": "UTC", "zero_value_orders": "include"}),
        "rules": json.dumps(rules),
        "metricRules": "{}",
        "limit": 100000,
    }
    report = polar.call("generate_report", payload)
    for key in (
        "filtersNotApplied",
        "dimensionsNotBound",
        "metricsWithDimension",
        "truncated",
        "rowLimitReached",
    ):
        if report.get(key):
            raise SourceIncomplete("Polar AFN query incomplete or requested scope not applied")
    # A separate provider totals query proves the report rows, independent of SKU grouping.
    control = polar.call("generate_report", payload | {"dimensions": [], "granularity": "none"})
    if control.get("filtersNotApplied") or control.get("truncated"):
        raise SourceIncomplete("Polar AFN provider control incomplete")
    described = polar.live("describe", {})
    operations = {r.get("operation") for r in described.get("rows", [])}
    if not {"sales", "inventory"} <= operations:
        raise SourceIncomplete("Polar Amazon live API contract changed")
    boundary = polar.live(
        "sales",
        {
            "start_date": start.isoformat(),
            "end_date": day.isoformat(),
            "granularity": "Day",
            "marketplace_ids": [MARKETPLACE],
            "timezone": "UTC",
            "limit": 500,
        },
    )
    validate_utc_intervals(boundary, start, day)
    raw = {
        "metric": METRIC,
        "query_timezone": "UTC",
        "marketplace": MARKETPLACE,
        "sales_channel": "Amazon.com",
        "fulfillment_channel": "AFN",
        "status_excluded": "Canceled",
        "date_from": start.isoformat(),
        "date_to": day.isoformat(),
        "rows": report.get("tableData"),
        "total": control.get("totalData"),
        "truncated": False,
        "row_limit_reached": False,
        "limit": payload["limit"],
        "utc_boundary_evidence": boundary,
        "provider_query": report.get("deepLink", ""),
        "fetched_at": datetime.now(UTC).isoformat(),
    }
    registry = {
        "live_kits": [
            {"sku": k.sku, "components": list(k.components)} for k in shiphero.fetch_all_kits()
        ],
        "bundles": [],
    }
    normalized = normalize_source(raw, registry, asset("catalog.json"))
    if report.get("totalData") != control.get("totalData"):
        raise SourceIncomplete("AFN provider total changed during capture")
    inventory = polar.live("inventory", {"marketplace_ids": [MARKETPLACE], "limit": 200})
    return {"raw": raw, "normalized": normalized, "inventory": inventory, "query": payload}


def publish_amazon(source, google, *, mode="compare", now=None, before_write=None):
    validate_mode(mode)
    tabs = {
        s["properties"]["sheetId"]: s["properties"]["title"]
        for s in google.metadata(DYNAMIC_ID).get("sheets", [])
    }
    if not {1740410293, 1740410294} <= tabs.keys():
        raise SourceIncomplete("Amazon source sheet identities missing")
    names = {key: "'" + value.replace("'", "''") + "'" for key, value in tabs.items()}
    old = google.values(DYNAMIC_ID, names[1740410294] + "!A9:Q")
    raw = source["raw"]
    end = date.fromisoformat(raw["date_to"])
    start = end - timedelta(days=6)
    plan = build_plan(
        source["inventory"],
        raw,
        asset("amazon_listing_annotations.json"),
        start.isoformat(),
        end.isoformat(),
        now=now,
        previous_count=len(old),
    )
    normalized = source["normalized"]["rows"]
    old_afn = google.values(DYNAMIC_ID, names[1740410293] + "!A2:E")
    # Preserve all verified history outside the newly authoritative replacement window.
    epoch = date(1899, 12, 30)
    lo = (date.fromisoformat(raw["date_from"]) - epoch).days
    hi = (end - epoch).days
    if any(len(r) < 5 or not isinstance(r[0], int | float) for r in old_afn):
        raise SourceIncomplete("Existing AFN history contains invalid dates/rows")
    rows = [r for r in old_afn if not lo <= r[0] <= hi] + [
        [
            (date.fromisoformat(r["date"]) - epoch).days,
            r["product"],
            r["units"],
            r["sku"],
            r["channel"],
        ]
        for r in normalized
    ]
    rows.sort(key=lambda r: (r[0], r[3]))
    if len({(r[0], r[3]) for r in rows}) != len(rows):
        raise SourceIncomplete("AFN history has duplicate date/SKU rows")
    req = {
        "updateCells": {
            "range": {
                "sheetId": 1740410293,
                "startRowIndex": 1,
                "endRowIndex": 1 + max(len(rows), len(old_afn)),
                "startColumnIndex": 0,
                "endColumnIndex": 5,
            },
            "rows": [{"values": [cell(v) for v in r]} for r in rows],
            "fields": "userEnteredValue",
        }
    }
    source_metadata = google.values(DYNAMIC_ID, names[1740410293] + "!G1:H14")
    by_label = {r[0]: r[1] for r in source_metadata if len(r) >= 2}
    try:
        covered_from = date.fromisoformat(by_label["Coverage from"])
        covered_through = date.fromisoformat(by_label["Coverage through"])
    except (KeyError, ValueError):
        raise SourceIncomplete("AFN authoritative coverage metadata is missing") from None
    if date.fromisoformat(raw["date_from"]) > covered_through + timedelta(days=1):
        raise SourceIncomplete("AFN refresh leaves a gap after existing authoritative coverage")
    if covered_through > end:
        raise SourceIncomplete("AFN source already covers a newer day")
    evidence = [
        ["Metric", "Physical units after kit expansion"],
        ["Metric", "Physical units after kit expansion"],
        ["Coverage from", min(covered_from, date.fromisoformat(raw["date_from"])).isoformat()],
        ["Coverage through", raw["date_to"]],
        ["Query timezone", "UTC"],
        ["Provider query", raw.get("provider_query", "")],
        ["Provider listing units (latest window)", source["normalized"]["ordered_listing_units"]],
        ["Physical units (entire verified history)", sum(r[2] for r in rows)],
        ["Fetched at UTC", raw["fetched_at"]],
        [
            "Scope",
            f"US Amazon.com; AFN; canceled excluded. Latest independently verified window {raw['date_from']} through {raw['date_to']}.",
        ],
        ["Source rows (latest window)", len(raw["rows"])],
        ["Response limit", raw["limit"]],
        ["Truncated", "false"],
        [
            "UTC boundary proof",
            f"{raw['date_from']} through {raw['date_to']}: complete provider UTC intervals",
        ],
    ]
    metadata_request = {
        "updateCells": {
            "start": {"sheetId": 1740410293, "rowIndex": 0, "columnIndex": 6},
            "rows": [{"values": [cell(v) for v in r]} for r in evidence],
            "fields": "userEnteredValue",
        }
    }
    requests = [req, metadata_request] + plan["batch_update"]["requests"]
    if mode == "publish" and before_write:
        before_write(
            {
                "requests": {DYNAMIC_ID: requests},
                "source_captured_at": source["inventory"]["fetched_at"],
                "date_from": raw["date_from"],
                "date_to": raw["date_to"],
            }
        )
    if mode == "publish":
        with contextlib.suppress(GoogleAPIError):
            google.batch(DYNAMIC_ID, requests)
    verify_requests(google, DYNAMIC_ID, requests)
    actual = google.values(
        DYNAMIC_ID, names[1740410293] + "!A2:E" + str(1 + max(len(rows), len(old_afn)))
    )
    if actual != rows:
        raise SourceIncomplete("AFN native readback differs from independently normalized source")
    readback = google.values(
        DYNAMIC_ID, names[1740410294] + "!A9:Q" + str(8 + max(len(old), plan["listing_count"]))
    )
    verify_readback(plan, readback)
    return {
        "status": "verified",
        "date_from": raw["date_from"],
        "date_to": raw["date_to"],
        "ordered_listing_units": source["normalized"]["ordered_listing_units"],
        "physical_units": source["normalized"]["physical_units"],
        "listing_count": plan["listing_count"],
        "inventory_captured_at": source["inventory"]["fetched_at"],
        "verification": {"requests": {DYNAMIC_ID: requests}},
    }
