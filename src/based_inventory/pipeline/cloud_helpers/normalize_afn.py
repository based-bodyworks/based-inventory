"""Expand verified Amazon fulfilled listing units to physical component SKUs.

This reads exported source data only. It cannot write a warehouse or a Sheet.
Use a fresh ShipHero kit registry and the live S&OP product map for every run.
"""

import argparse
import collections
import datetime as dt
import json
import math
import re
from pathlib import Path

METRIC = "amazonsp_order_items.raw.quantity_ordered"


def base_sku(sku):
    sku = sku.strip()
    sku = re.sub(r"-stickerless$", "", sku, flags=re.I)
    return re.sub(r"\s*-\s*(?:FBA|FBM)(?:\s*-\s*(?:FNSKU|NEW|\d+))?$", "", sku, flags=re.I).strip()


def normalize(rows, registry, catalog):
    aliases = collections.defaultdict(set)
    primary = {}
    for item in catalog:
        if not item.get("skus"):
            continue
        primary[item["product"]] = item["skus"][0]
        for sku in item["skus"]:
            aliases[sku].add(item["product"])
    kits = {x["sku"]: x["components"] for x in registry.get("live_kits", [])}
    for bundle in registry.get("bundles", []):
        if bundle["bundle_sku"] not in kits:
            if bundle.get("partially_resolved"):
                continue
            kits[bundle["bundle_sku"]] = [(x[0], x[2]) for x in bundle["components_resolved"]]

    def resolve(sku, trail=()):
        candidates = list(dict.fromkeys([sku, base_sku(sku)]))
        # Kit definitions override a legacy lookup that mapped an entire kit
        # to only its first component (for example the old Skincare Duo map).
        for key in candidates:
            if key in kits:
                if key in trail:
                    raise ValueError("cyclic kit: " + key)
                result = []
                for component, factor in kits[key]:
                    if not component or not isinstance(factor, int | float) or factor <= 0:
                        raise ValueError("unresolved kit component: " + key)
                    result.extend(
                        (p, s, n * factor) for p, s, n in resolve(component, (*trail, key))
                    )
                return result
        for key in candidates:
            products = aliases.get(key, set())
            if len(products) > 1:
                raise ValueError("ambiguous SKU: " + sku)
            if products:
                product = next(iter(products))
                return [(product, primary[product], 1)]
        raise ValueError("unmapped SKU: " + sku)

    seen = set()
    totals = collections.defaultdict(int)
    crosswalk = {}
    ordered_units = 0
    for row in rows:
        day = dt.date.fromisoformat(row["date"]).isoformat()
        sku = str(row["sku"]).strip()
        quantity = row.get(METRIC, row.get("units"))
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int | float)
            or not math.isfinite(quantity)
            or quantity < 0
            or int(quantity) != quantity
        ):
            raise ValueError("invalid quantity for SKU: " + sku)
        key = (day, sku)
        if key in seen:
            raise ValueError("duplicate date/SKU: " + repr(key))
        seen.add(key)
        components = resolve(sku)
        crosswalk[sku] = [{"product": p, "sku": s, "factor": n} for p, s, n in components]
        ordered_units += int(quantity)
        for product, canonical_sku, factor in components:
            units = quantity * factor
            if int(units) != units:
                raise ValueError("noninteger physical quantity for SKU: " + sku)
            totals[(day, product, canonical_sku)] += int(units)
    normalized = [
        {"date": d, "product": p, "sku": s, "units": units, "channel": "AMAZON FBA"}
        for (d, p, s), units in sorted(totals.items())
    ]
    return {
        "rows": normalized,
        "crosswalk": crosswalk,
        "ordered_listing_units": ordered_units,
        "physical_units": sum(x["units"] for x in normalized),
        "source_row_count": len(rows),
    }


def normalize_source(raw, registry, catalog):
    expected = {
        "query_timezone": "UTC",
        "marketplace": "ATVPDKIKX0DER",
        "sales_channel": "Amazon.com",
        "fulfillment_channel": "AFN",
        "status_excluded": "Canceled",
        "metric": METRIC,
    }
    for field, value in expected.items():
        if raw.get(field) != value:
            raise ValueError("invalid source scope: " + field)
    rows = raw.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("empty source rows")
    if (
        raw.get("truncated")
        or raw.get("rowLimitReached")
        or raw.get("row_limit_reached")
        or raw.get("has_more")
        or raw.get("next_page_token")
    ):
        raise ValueError("truncated source query")
    limit = raw.get("limit", raw.get("row_limit"))
    if limit is not None and len(rows) >= limit:
        raise ValueError("source may be truncated at row limit")
    totals = raw.get("total")
    if not isinstance(totals, list) or len(totals) != 1:
        raise ValueError("independent provider total is missing")
    provider_total = totals[0].get(METRIC)
    if (
        isinstance(provider_total, bool)
        or not isinstance(provider_total, int | float)
        or not math.isfinite(provider_total)
        or provider_total < 0
        or int(provider_total) != provider_total
    ):
        raise ValueError("invalid independent provider total")
    start = dt.date.fromisoformat(raw["date_from"])
    end = dt.date.fromisoformat(raw["date_to"])
    if end < start:
        raise ValueError("invalid source date window")
    dates = {dt.date.fromisoformat(row["date"]) for row in rows}
    required = {start + dt.timedelta(days=i) for i in range((end - start).days + 1)}
    if dates - required:
        raise ValueError("source date outside requested window")
    if required - dates:
        raise ValueError(
            "missing source date: " + ", ".join(str(x) for x in sorted(required - dates))
        )
    result = normalize(rows, registry, catalog)
    if result["ordered_listing_units"] != provider_total:
        raise ValueError("provider total does not match source rows")
    result["provider_listing_units"] = int(provider_total)
    result["verified_calendar_days"] = len(required)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw", type=Path, required=True)
    p.add_argument("--registry", type=Path, required=True)
    p.add_argument("--catalog", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--sheets-rows", type=Path)
    args = p.parse_args()
    raw = json.loads(args.raw.read_text())
    result = normalize_source(
        raw, json.loads(args.registry.read_text()), json.loads(args.catalog.read_text())
    )
    result["source"] = {k: v for k, v in raw.items() if k != "rows"}
    result["normalized_at_utc"] = dt.datetime.now(dt.UTC).isoformat()
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.sheets_rows:
        epoch = dt.date(1899, 12, 30)
        values = [
            [
                (dt.date.fromisoformat(x["date"]) - epoch).days,
                x["product"],
                x["units"],
                x["sku"],
                x["channel"],
            ]
            for x in result["rows"]
        ]
        args.sheets_rows.write_text(json.dumps(values) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ["rows", "crosswalk", "source"]}))


if __name__ == "__main__":
    main()
