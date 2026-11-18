"""Read-only, date-bound platform sales with physical component quantities.

Online platforms use the actual Polar workspace calendar day. Target uses its
reported retail date. Warehouse order snapshots never enter these totals.
"""

from __future__ import annotations

import base64
import binascii
import csv
import gzip
import io
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from based_inventory.pipeline.cloud_helpers.normalize_afn import base_sku
from based_inventory.pipeline.cloud_helpers.parse_target_report import normalized
from based_inventory.pipeline.models import SalesRow, canonical, digest
from based_inventory.pipeline.target_refresh import SourceIncomplete, asset, collect_target
from based_inventory.registry import build_registry
from based_inventory.shiphero import MERCHDROP_WAREHOUSE_ID

CHANNELS = ("Shopify", "TikTok", "Amazon", "Target")
METRICS = {
    "Shopify": "shopify_sales_main.raw.net_product_quantity",
    "TikTok": "tiktokshop_orders.raw.quantity_tiktok",
    "Amazon": "amazonsp_order_items.raw.quantity_ordered",
}
MAX_ROWS = 100_000
MAX_RAW_BYTES = 64 * 1024 * 1024
COMPONENTS_PATH = Path(__file__).resolve().parents[2] / "data/set-components.json"


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_thaw(v) for v in value]
    return value


def encode_proof(value) -> tuple[str, str]:
    raw = canonical(value)
    if len(raw) > MAX_RAW_BYTES:
        raise SourceIncomplete("Sales source evidence exceeds its safe size limit")
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode(), digest(raw)


def decode_source_proof(source):
    try:
        encoded = source["raw_proof_gzip"]
        if not isinstance(encoded, str) or len(encoded) > MAX_RAW_BYTES * 2:
            raise ValueError("Invalid compressed source evidence")
        compressed = base64.b64decode(encoded, validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(MAX_RAW_BYTES + 1)
        if len(raw) > MAX_RAW_BYTES or digest(raw) != source["raw_sha256"]:
            raise ValueError("Sales source evidence hash or size mismatch")
        value = json.loads(raw)
        if canonical(value) != raw:
            raise ValueError("Non-canonical sales source evidence")
        return value
    except (KeyError, TypeError, binascii.Error, EOFError, OSError, json.JSONDecodeError):
        raise ValueError("Invalid compressed sales source evidence") from None


def _quantity(value, *, label="sales quantity") -> int:
    if isinstance(value, bool):
        raise SourceIncomplete(f"Invalid {label}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise SourceIncomplete(f"Invalid {label}") from None
    if not number.is_finite() or number < 0 or int(number) != number:
        raise SourceIncomplete(f"Invalid {label}")
    return int(number)


def _end_of_day(day: date, timezone: str) -> datetime:
    try:
        return datetime.combine(day + timedelta(days=1), time(), ZoneInfo(timezone))
    except (ValueError, ZoneInfoNotFoundError):
        raise SourceIncomplete("Sales reporting timezone is unavailable") from None


@dataclass(frozen=True)
class SalesDay:
    day: date
    rows: tuple[SalesRow, ...]
    sources: Mapping
    timezone: str

    def __post_init__(self):
        if type(self.day) is not date or not isinstance(self.timezone, str):
            raise ValueError("Invalid sales report date or timezone")
        _end_of_day(self.day, self.timezone)
        sources = _thaw(self.sources)
        if not isinstance(sources, dict) or set(sources) != set(CHANNELS):
            raise ValueError("All four sales sources are required")
        rows = tuple(sorted(self.rows, key=lambda r: (r.sku, r.channel)))
        seen = set()
        for row in rows:
            if (
                not isinstance(row, SalesRow)
                or not row.sku
                or not row.product
                or row.channel not in CHANNELS
                or type(row.units) is not int
                or row.units < 0
            ):
                raise ValueError("Invalid all-channel sales row")
            if (row.sku, row.channel) in seen:
                raise ValueError("Duplicate all-channel sales identity")
            seen.add((row.sku, row.channel))
        for channel, source in sources.items():
            if not isinstance(source, dict) or source.get("status") != "complete":
                raise ValueError("Incomplete sales sources cannot become a sales day")
            if source.get("date") != self.day.isoformat():
                raise ValueError("Sales source date differs from reporting date")
            expected_zone = "retail_report_date" if channel == "Target" else self.timezone
            if source.get("timezone") != expected_zone:
                raise ValueError("Sales source timezone differs from its reporting clock")
            try:
                captured = datetime.fromisoformat(source["captured_at"].replace("Z", "+00:00"))
                normalized_rows = tuple(SalesRow(**row) for row in source["normalized_rows"])
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ValueError("Invalid saved sales source metadata") from None
            if captured.tzinfo is None or (
                channel != "Target" and captured < _end_of_day(self.day, self.timezone)
            ):
                raise ValueError("Sales source date is not complete")
            expected_rows = tuple(row for row in rows if row.channel == channel)
            if tuple(sorted(normalized_rows, key=lambda r: (r.sku, r.channel))) != expected_rows:
                raise ValueError("Sales source normalized rows differ from report rows")
            if source.get("component_units") != sum(row.units for row in expected_rows):
                raise ValueError("Sales source component units do not reconcile")
            if not isinstance(source.get("issues", []), list) or any(
                not isinstance(issue, str) or not issue for issue in source.get("issues", [])
            ):
                raise ValueError("Invalid sales source issue list")
            proof = decode_source_proof(source)
            if not isinstance(proof, dict):
                raise ValueError("Invalid sales source proof")
            integrity = source.get("integrity", {})
            if (
                not isinstance(integrity, dict)
                or integrity != proof.get("integrity", {})
                or any(
                    isinstance(verdict, dict) and verdict.get("state") == "mismatched"
                    for verdict in integrity.values()
                )
            ):
                raise ValueError("Saved sales source integrity is mismatched or differs from proof")
            if source.get("response_limits", {}) != proof.get("response_limits", {}):
                raise ValueError("Saved sales source response limits differ from proof")
            for key in ("listing_units", "control_listing_units", "source_row_count"):
                if type(source.get(key)) is not int or source[key] < 0:
                    raise ValueError("Invalid saved sales source control")
            if source["listing_units"] != source["control_listing_units"]:
                raise ValueError("Saved sales source listing/control units differ")
            if channel == "Target":
                parsed = proof.get("parsed", {})
                products = parsed.get("products")
                if (
                    not isinstance(products, list)
                    or parsed.get("date") != str(self.day)
                    or len(products) != source["source_row_count"]
                    or sum(_quantity(p.get("daily_units")) for p in products)
                    != source["listing_units"]
                    or parsed.get("derived_daily_units") != source["component_units"]
                ):
                    raise ValueError("Saved Target source control does not reconcile")
                for key in (
                    "reported_daily_units",
                    "derived_daily_units",
                    "units_variance",
                    "message_id",
                    "previous_message_id",
                ):
                    if source.get(key) != parsed.get(key):
                        raise ValueError(
                            "Saved Target displayed metadata differs from source proof"
                        )
            else:
                raw_rows = proof.get("rows")
                if (
                    not isinstance(raw_rows, list)
                    or len(raw_rows) != source["source_row_count"]
                    or sum(_quantity(row.get("units")) for row in raw_rows)
                    != source["listing_units"]
                    or proof.get("control_listing_units") != source["control_listing_units"]
                ):
                    raise ValueError("Saved online source control does not reconcile")
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "sources", _freeze(sources))

    @property
    def totals(self) -> dict[str, int]:
        return {
            channel: sum(r.units for r in self.rows if r.channel == channel) for channel in CHANNELS
        }

    @property
    def total_units(self) -> int:
        return sum(row.units for row in self.rows)

    @property
    def issues(self) -> dict[str, tuple[str, ...]]:
        return {channel: tuple(self.sources[channel].get("issues", ())) for channel in CHANNELS}

    @property
    def complete(self) -> bool:
        return not any(self.issues.values())

    def payload(self) -> dict:
        return {
            "schema": 1,
            "day": self.day.isoformat(),
            "timezone": self.timezone,
            "rows": [asdict(row) for row in self.rows],
            "sources": _thaw(self.sources),
        }

    @property
    def capture_id(self) -> str:
        return digest(canonical(self.payload()))

    @classmethod
    def from_payload(cls, payload: dict):
        if not isinstance(payload, dict) or payload.get("schema") != 1:
            raise ValueError("Unsupported all-channel sales schema")
        try:
            result = cls(
                date.fromisoformat(payload["day"]),
                tuple(SalesRow(**row) for row in payload["rows"]),
                payload["sources"],
                payload["timezone"],
            )
        except (KeyError, TypeError):
            raise ValueError("Invalid saved all-channel sales payload") from None
        if result.payload() != payload:
            raise ValueError("Non-canonical all-channel sales payload")
        return result


def _marketplace_mirror(label: str) -> bool:
    text = label.casefold().strip()
    return (
        "tiktok" in text
        or text == "tts"
        or text in {"amazon", "amazon.com", "amazon.ca", "amazon.com.mx", "amazon-us"}
    )


def normalize_online_rows(records, channel, registry, known_kit_skus, inventory, aliases):
    """Normalize native per-order listing rows, not an already summed SKU grid."""
    if channel not in METRICS:
        raise SourceIncomplete("Unknown online sales channel")
    listing_units = defaultdict(int)
    seen = set()
    names = {}
    unknown = set()
    excluded = 0
    for row in records:
        units = _quantity(row.get("units"))
        if units == 0:
            continue
        order_id, raw_sku = (
            str(row.get("order_id") or "").strip(),
            str(row.get("sku") or "").strip(),
        )
        if not order_id or not raw_sku:
            raise SourceIncomplete("Positive sales row has no order/SKU identity")
        key = (order_id, raw_sku)
        if key in seen:
            raise SourceIncomplete("Duplicate native order/SKU sales row")
        seen.add(key)
        if channel == "Shopify" and _marketplace_mirror(str(row.get("sales_channel_name") or "")):
            excluded += units
            continue
        candidates = list(dict.fromkeys((raw_sku, base_sku(raw_sku))))
        sku = next(
            (s for s in candidates if s in registry.bundle_skus or s in known_kit_skus), None
        )
        if sku is None:
            sku = next((s for s in candidates if s in inventory), None)
        if sku is None:
            for candidate in candidates:
                matches = aliases.get(candidate, set())
                if len(matches) > 1:
                    raise SourceIncomplete("Ambiguous physical SKU alias")
                if matches:
                    sku = next(iter(matches))
                    break
        if sku is None:
            sku = raw_sku
            unknown.add(sku)
            names[sku] = str(row.get("product_title") or f"Unmapped SKU {sku}")
        listing_units[sku] += units

    resolved = {}
    resolving = set()

    def components(sku):
        if sku not in registry.bundle_skus and sku not in known_kit_skus:
            return {sku: 1}
        if sku in resolved:
            return resolved[sku]
        entry = registry.by_bundle_sku.get(sku)
        if (
            sku in resolving
            or entry is None
            or entry.partially_resolved
            or not entry.components_resolved
        ):
            raise SourceIncomplete("Native order bundle components could not be verified")
        resolving.add(sku)
        leaves = defaultdict(int)
        try:
            for child, _name, count in entry.components_resolved:
                count = _quantity(count, label="bundle component quantity")
                if not child or not count:
                    raise SourceIncomplete("Native order bundle components could not be verified")
                for leaf, quantity in components(child).items():
                    leaves[leaf] += count * quantity
        finally:
            resolving.remove(sku)
        resolved[sku] = dict(leaves)
        return resolved[sku]

    # Native sales rows are as-sold listings. A kit and a loose component in
    # the same cart are separate purchases. Warehouse marker heuristics must
    # not suppress either line; expand every sold listing independently.
    physical_units = defaultdict(int)
    for sku, units in listing_units.items():
        for leaf, count in components(sku).items():
            physical_units[leaf] += units * count
    totals = defaultdict(int)
    for sku, units in physical_units.items():
        canonical_sku = sku
        if sku not in inventory:
            matches = aliases.get(sku, aliases.get(base_sku(sku), set()))
            if len(matches) > 1:
                raise SourceIncomplete("Ambiguous physical component alias")
            if matches:
                canonical_sku = next(iter(matches))
            else:
                unknown.add(sku)
        totals[canonical_sku] += units
    rows = tuple(
        SalesRow(sku, inventory.get(sku, names.get(sku, f"Unmapped SKU {sku}")), channel, units)
        for sku, units in sorted(totals.items())
    )
    return rows, [f"Unmapped physical SKU: {sku}" for sku in sorted(unknown)], excluded


def _download_row_bound(download):
    if not isinstance(download, dict):
        raise SourceIncomplete("Polar complete download is unavailable")
    try:
        parsed = urlparse(download.get("url", ""))
        port = parsed.port
    except (TypeError, ValueError):
        raise SourceIncomplete("Unexpected Polar download origin or format") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.polaranalytics.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or download.get("format") != "csv"
    ):
        raise SourceIncomplete("Unexpected Polar download origin or format")
    max_rows = download.get("maxRows")
    if type(max_rows) is not int or max_rows < 1 or max_rows > MAX_ROWS:
        raise SourceIncomplete("Invalid Polar download row bound")
    return max_rows


def download_report_rows(download: dict, *, session=None):
    max_rows = _download_row_bound(download)
    own_session = session is None
    session = session or requests.Session()
    try:
        with session.get(
            download["url"], timeout=60, stream=True, allow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise SourceIncomplete("Polar full-row download unavailable")
            raw = bytearray()
            for chunk in response.iter_content(64 * 1024):
                raw.extend(chunk)
                if len(raw) > MAX_RAW_BYTES:
                    raise SourceIncomplete("Polar full-row download exceeds its safe size bound")
        reader = csv.DictReader(io.StringIO(bytes(raw).decode("utf-8-sig")))
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise SourceIncomplete("Invalid Polar full-row CSV header")
        rows = list(reader)
        if len(rows) >= max_rows or len(rows) >= MAX_ROWS:
            raise SourceIncomplete("Polar full-row download may be truncated")
        return rows, digest(bytes(raw))
    except (requests.RequestException, UnicodeError, csv.Error):
        raise SourceIncomplete("Polar full-row download could not be read") from None
    finally:
        if own_session:
            session.close()


def _report_metadata(report, metric, day, timezone, *, download_row_bound=None):
    _integrity_verdict(report, metric)
    for key in (
        "filtersNotApplied",
        "dimensionsNotBound",
        "metricsWithDimension",
        "metricCoverage",
    ):
        if report.get(key):
            raise SourceIncomplete("Online source requested scope is incomplete or not applied")
    if limit := report.get("rowLimitReached"):
        inline_cap = limit.get("limit") if isinstance(limit, dict) else None
        if (
            type(inline_cap) is not int
            or inline_cap < 1
            or download_row_bound is None
            or inline_cap >= download_row_bound
        ):
            raise SourceIncomplete("Online source row limit has no larger complete download")
    if report.get("dateRange", {}).get("start") != str(day) or report.get("dateRange", {}).get(
        "end"
    ) != str(day):
        raise SourceIncomplete("Online source returned a different report date")
    through = report.get("dataThrough", {}).get(metric)
    try:
        captured_through = datetime.fromisoformat(through.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise SourceIncomplete("Online source freshness proof is unavailable") from None
    if captured_through.tzinfo is None or captured_through < _end_of_day(day, timezone):
        raise SourceIncomplete("Online source has not loaded the complete reporting day")


def _integrity_verdict(report, metric):
    integrity = report.get("integrity")
    if integrity is None:
        return None
    if not isinstance(integrity, dict):
        raise SourceIncomplete("Online source integrity metadata is invalid")
    verdict = integrity.get(metric)
    if verdict is not None and not isinstance(verdict, dict):
        raise SourceIncomplete("Online source integrity metadata is invalid")
    if verdict is not None and verdict.get("state") == "mismatched":
        raise SourceIncomplete("Online source integrity check reports a mismatch")
    return verdict


def collect_online_source(
    day, channel, polar, timezone, registry, known_kit_skus, inventory, aliases
):
    metric = METRICS[channel]
    if datetime.now(UTC) < _end_of_day(day, timezone):
        raise SourceIncomplete("Online sales require a complete source calendar day")
    dimensions = polar.call(
        "list_dimensions", {"conversation_id": polar.conversation, "metrics": metric}
    )
    # Provider-side grouping must keep exactly one row per order/SKU. Product
    # titles can differ for the same SKU in one cart and would split that key.
    required_dimensions = {"order_id", "sku"}
    required_filters = {"is_cancelled"}
    if channel == "Shopify":
        required_dimensions.add("sales_channel_name")
        required_filters.add("is_gift_card")
    if not required_dimensions <= set(
        dimensions.get("dimensions", [])
    ) or not required_filters <= set(dimensions.get("filters", [])):
        raise SourceIncomplete("Online platform dimension contract changed")
    rules = {"is_cancelled": [{"operator": "IS", "value": ["false"]}]}
    if channel == "Shopify":
        rules["is_gift_card"] = [{"operator": "IS", "value": ["false"]}]
    payload = {
        "conversation_id": polar.conversation,
        "metrics": metric,
        "dimensions": sorted(required_dimensions),
        "dateRangeFrom": str(day),
        "dateRangeTo": str(day),
        "granularity": "none",
        "ordering": "order_id:ASC",
        "rules": json.dumps(rules),
        "metricRules": "{}",
        "settings": json.dumps({"zero_value_orders": "include"}),
        "limit": MAX_ROWS,
    }
    report = polar.call("generate_report", payload)
    control_query = payload | {"dimensions": [], "ordering": metric + ":DESC", "limit": 10}
    control = polar.call("generate_report", control_query)
    full_download = report.get("download")
    needs_download = any(
        report.get(key) for key in ("truncated", "tableDataCutForSize", "rowLimitReached")
    )
    download_bound = _download_row_bound(full_download) if needs_download else None
    _report_metadata(report, metric, day, timezone, download_row_bound=download_bound)
    _report_metadata(control, metric, day, timezone)
    if control.get("truncated") or control.get("tableDataCutForSize"):
        raise SourceIncomplete("Online source control is incomplete")
    if needs_download:
        raw_rows, transport_hash = download_report_rows(full_download)
    else:
        raw_rows = report.get("tableData")
        transport_hash = digest(canonical(raw_rows))
    if not isinstance(raw_rows, list) or len(raw_rows) >= MAX_ROWS:
        raise SourceIncomplete("Online source rows are unavailable or truncated")
    if needs_download and len(raw_rows) >= download_bound:
        raise SourceIncomplete("Online source complete download may be truncated")
    response_limits = {
        "truncated": report.get("truncated", False),
        "rowLimitReached": report.get("rowLimitReached"),
        "tableDataCutForSize": report.get("tableDataCutForSize"),
        "inline_row_count": len(report["tableData"])
        if isinstance(report.get("tableData"), list)
        else None,
        "download_max_rows": download_bound,
        "downloaded_row_count": len(raw_rows) if needs_download else None,
    }
    totals = control.get("totalData")
    if not isinstance(totals, list) or len(totals) != 1 or metric not in totals[0]:
        raise SourceIncomplete("Online source independent control is unavailable")
    total = _quantity(totals[0][metric], label="provider control quantity")
    records = []
    for row in raw_rows:
        if not isinstance(row, dict) or metric not in row:
            raise SourceIncomplete("Invalid online source row")
        records.append(
            {
                "order_id": row.get("order_id"),
                "sku": row.get("sku"),
                "units": _quantity(row[metric]),
                "product_title": row.get("product_title"),
                "sales_channel_name": row.get("sales_channel_name"),
            }
        )
    listing_units = sum(row["units"] for row in records)
    if listing_units != total:
        raise SourceIncomplete("Online source rows do not match independent control")
    rows, issues, excluded = normalize_online_rows(
        records, channel, registry, known_kit_skus, inventory, aliases
    )
    integrity = {
        label: verdict
        for label, result in (("report", report), ("control", control))
        if (verdict := _integrity_verdict(result, metric)) is not None
    }
    proof, proof_hash = encode_proof(
        {
            "rows": records,
            "control_listing_units": total,
            "query": payload,
            "control_query": control_query,
            "source_timezone": timezone,
            "data_through": report["dataThrough"][metric],
            "registry": [asdict(entry) for entry in registry.bundles],
            "known_kit_skus": sorted(known_kit_skus),
            "inventory": inventory,
            "aliases": {key: sorted(values) for key, values in aliases.items()},
            "integrity": integrity,
            "response_limits": response_limits,
        }
    )
    return {
        "status": "complete",
        "date": str(day),
        "timezone": timezone,
        "captured_at": datetime.now(UTC).isoformat(),
        "metric": metric,
        "normalized_rows": [asdict(row) for row in rows],
        "component_units": sum(row.units for row in rows),
        "listing_units": listing_units,
        "control_listing_units": total,
        "source_row_count": len(records),
        "raw_proof_gzip": proof,
        "raw_sha256": proof_hash,
        "transport_sha256": transport_hash,
        "provider_query": report.get("deepLink", ""),
        "data_through": report["dataThrough"][metric],
        "integrity": integrity,
        "response_limits": response_limits,
        "excluded_mirrored_listing_units": excluded,
        "source_labels": sorted(
            {str(r.get("sales_channel_name")) for r in records if r.get("sales_channel_name")}
        ),
        "scope": {
            "cancelled_orders": "excluded",
            "returns": "not deducted",
            "zero_value_orders": "included",
            "gift_cards": "excluded" if channel == "Shopify" else "not applicable",
            "fulfillment": "all",
        },
        "issues": issues,
    }


def normalize_target_source(day, parsed, inventory, bindings):
    if parsed.get("date") != str(day):
        raise SourceIncomplete("Target source date differs from reporting date")
    if parsed.get("safe_to_write") is not True or parsed.get("unmapped_products"):
        raise SourceIncomplete("Target product coverage is incomplete")
    by_label = {}
    for binding in bindings:
        key = normalized(binding["report_label"])
        if key in by_label:
            raise SourceIncomplete("Duplicate Target retail binding")
        by_label[key] = binding
    totals = defaultdict(int)
    seen = set()
    products = parsed.get("products")
    if not isinstance(products, list) or not products or not parsed.get("message_id"):
        raise SourceIncomplete("Target complete product report is unavailable")
    for item in products:
        key = normalized(item.get("label", ""))
        binding = by_label.get(key)
        if key in seen or not binding or item.get("sku") != binding.get("report_sku"):
            raise SourceIncomplete("Target product identity is missing, duplicate or changed")
        seen.add(key)
        sku = binding.get("inventory_sku")
        if sku not in inventory:
            raise SourceIncomplete("Target retail packaging SKU is absent from inventory catalog")
        totals[sku] += _quantity(item.get("daily_units"), label="Target daily units")
    derived = _quantity(parsed.get("derived_daily_units"), label="Target derived units")
    if sum(totals.values()) != derived:
        raise SourceIncomplete("Target product daily units do not reconcile")
    rows = tuple(
        SalesRow(sku, inventory[sku], "Target", units) for sku, units in sorted(totals.items())
    )
    proof, proof_hash = encode_proof({"parsed": parsed, "bindings": bindings})
    return {
        "status": "complete",
        "date": str(day),
        "timezone": "retail_report_date",
        "captured_at": datetime.now(UTC).isoformat(),
        "normalized_rows": [asdict(row) for row in rows],
        "component_units": derived,
        "listing_units": derived,
        "control_listing_units": derived,
        "source_row_count": len(products),
        "raw_proof_gzip": proof,
        "raw_sha256": proof_hash,
        "issues": [],
        "reported_daily_units": parsed.get("reported_daily_units"),
        "derived_daily_units": derived,
        "units_variance": parsed.get("units_variance"),
        "message_id": parsed["message_id"],
        "previous_message_id": parsed.get("previous_message_id"),
        "provider_query": "https://mail.google.com/mail/u/0/#all/" + parsed["message_id"],
        "scope": {"measure": "retail product units sold", "date": "Target report calendar date"},
    }


def collect_sales_day(day: date, polar, shiphero, google, target_query: str) -> SalesDay:
    context = polar.context()
    timezone = context.get("context", {}).get("timezone")
    if not isinstance(timezone, str) or datetime.now(UTC) < _end_of_day(day, timezone):
        raise SourceIncomplete("Complete online report day/timezone is unavailable")
    inventory = {}
    product_skus = {}
    for item in asset("inventory_catalog.json"):
        if item["sku"] in inventory or item["product"] in product_skus:
            raise SourceIncomplete("Physical inventory catalog identity is ambiguous")
        inventory[item["sku"]] = item["product"]
        product_skus[item["product"]] = item["sku"]
    aliases = defaultdict(set)
    for item in asset("catalog.json"):
        if item["product"] not in product_skus:
            raise SourceIncomplete("Product alias is absent from physical inventory catalog")
        for sku in item.get("skus", []):
            aliases[sku].add(product_skus[item["product"]])
    try:
        kits = shiphero.fetch_all_kits(require_complete=True)
        stock = shiphero.fetch_warehouse_stock(
            warehouse_id=MERCHDROP_WAREHOUSE_ID, require_complete=True
        )
    except RuntimeError:
        raise SourceIncomplete("ShipHero complete product and kit catalog is unavailable") from None
    registry = build_registry(kits, stock, COMPONENTS_PATH)
    known_kit_skus = {kit.sku for kit in kits} | {row.sku for row in stock if row.is_kit}
    sources = {
        channel: collect_online_source(
            day, channel, polar, timezone, registry, known_kit_skus, inventory, aliases
        )
        for channel in METRICS
    }
    target = collect_target(day, google, target_query)
    sources["Target"] = normalize_target_source(
        day, target, inventory, asset("target_inventory_bindings.json")["bindings"]
    )
    rows = tuple(
        SalesRow(**row) for source in sources.values() for row in source["normalized_rows"]
    )
    return SalesDay(day, rows, sources, timezone)
