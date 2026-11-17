"""Read ShipHero stock without changing inventory. Emit a Sheets write payload.

The API credentials stay in the existing based-inventory environment file.
This program only queries ShipHero. Publishing its emitted requests to the
existing Google workbook is a separate, explicit connector action.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

MERCHDROP_WAREHOUSE_ID = "V2FyZWhvdXNlOjExNzY2MQ=="
JUSTIN_SPREADSHEET_ID = "1TE-gFZLELr1BYfaTkhaAqhSkvuid7a8X0cZthyA33I4"
CURRENT_INVENTORY_SHEET_ID = 1740410290
CURRENT_INVENTORY_TABLE_ID = "1283118671"
API_URL = "https://public-api.shiphero.com/graphql"
QUANTITIES = ("on_hand", "available", "allocated", "reserve_inventory", "backorder", "sell_ahead")


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("collection timestamp must include a timezone")
    return value.astimezone(UTC)


def iso(value):
    return utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_target_inventory_coverage(catalog, target_product_map):
    """Require every Target SKU in the shared warehouse catalog, exactly once.

    Target's retail report is a sales source, not an instruction to reduce a
    current ShipHero balance. ShipHero already accounts for warehouse changes.
    """
    if not isinstance(target_product_map, dict) or not target_product_map:
        raise ValueError("Target product map must be a nonempty mapping")
    by_sku = {}
    for item in catalog:
        sku = item.get("sku")
        if sku:
            if sku in by_sku:
                raise ValueError(f"duplicate inventory catalog SKU: {sku}")
            by_sku[sku] = item
    skus = set()
    for label, item in target_product_map.items():
        sku = item.get("sku") if isinstance(item, dict) else None
        if not isinstance(sku, str) or not sku.strip():
            raise ValueError(f"Target product has no mapped SKU: {label}")
        if sku not in by_sku:
            raise ValueError(f"Target SKU missing from shared inventory catalog: {sku}")
        if by_sku[sku].get("required") is not True:
            raise ValueError(f"Target inventory SKU must be required: {sku}")
        skus.add(sku)
    return {
        "status": "Mapped catalog coverage only",
        "product_count": len(skus),
        "skus": sorted(skus),
        "target_warehouse_identity_verified": False,
        "scope": "Checks existing product-map coverage. Does not verify separate retail packaging SKUs or warehouse outbound.",
    }


def validate_target_inventory_bindings(
    catalog, target_product_map, bindings, warehouse_rows, warehouse_id
):
    """Validate retail identities separately from Gmail's canonical sales map."""
    records = bindings.get("bindings") if isinstance(bindings, dict) else None
    if not isinstance(records, list):
        raise ValueError("Target inventory bindings must contain a list")
    labels = [r.get("report_label") for r in records]
    if len(labels) != len(set(labels)) or set(labels) != set(target_product_map):
        raise ValueError("Target inventory binding coverage differs from current report map")
    catalog_by_sku = {p.get("sku"): p for p in catalog if p.get("sku")}
    rows_by_sku = {}
    for row in warehouse_rows:
        if row.get("warehouse_id") == warehouse_id:
            rows_by_sku.setdefault(row.get("sku"), []).append(row)
    used, views = set(), []
    for record in records:
        label = record["report_label"]
        report = target_product_map[label]
        if record.get("report_sku") != report["sku"]:
            raise ValueError(f"Target binding report SKU changed: {label}")
        status = record.get("identity_status")
        view = {
            "report_label": label,
            "product": report["product"],
            "report_sku": report["sku"],
            "inventory_sku": None,
            "identity_status": status,
            "provider_product_name": None,
            "on_hand": None,
            "available": None,
            "allocated": None,
            "backorder": None,
            "candidate_inventory_sku": record.get("candidate_inventory_sku"),
            "evidence": record.get("evidence"),
        }
        if status == "unresolved":
            if record.get("inventory_sku") is not None or not record.get("evidence"):
                raise ValueError(
                    f"Unresolved Target binding needs an explicit gap and no inventory SKU: {label}"
                )
            views.append(view)
            continue
        if status not in ("verified_retail_alias", "verified_target_order"):
            raise ValueError(f"Invalid Target identity status: {label}")
        sku = record.get("inventory_sku")
        if status == "verified_target_order":
            order = record.get("verified_order") or {}
            if (
                not order.get("id")
                or not str(order.get("order_number") or "").startswith("TARGET-")
                or order.get("fulfillment_status") != "fulfilled"
                or order.get("sku") != sku
            ):
                raise ValueError(
                    f"A fulfilled Target order with the exact SKU is required: {label}"
                )
        item = catalog_by_sku.get(sku)
        if not item or item.get("required") is not True or sku in used:
            raise ValueError(
                f"Target retail inventory SKU missing, duplicated or not required: {sku}"
            )
        used.add(sku)
        rows = rows_by_sku.get(sku, [])
        if len(rows) != 1:
            raise ValueError(f"Target retail warehouse row missing or duplicated: {sku}")
        row = rows[0]
        product = row.get("product") or {}
        if record.get("provider_kit") is not False or product.get("kit") is not False:
            raise ValueError(f"Target retail binding cannot use a kit: {sku}")
        title = record.get("provider_product_name")
        if not isinstance(title, str) or title.strip() != str(product.get("name") or "").strip():
            raise ValueError(f"Target retail provider title changed: {sku}")
        if not record.get("evidence"):
            raise ValueError(f"Target retail identity lacks evidence: {sku}")
        view.update({"inventory_sku": sku, "provider_product_name": product["name"]})
        for key in ("on_hand", "available", "allocated", "backorder"):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Invalid Target inventory quantity: {sku} / {key}")
            view[key] = value
        views.append(view)
    unresolved = sum(v["identity_status"] == "unresolved" for v in views)
    return {
        "status": "Verified retail mapping" if not unresolved else "Partial retail mapping",
        "product_count": len(views),
        "verified_count": len(views) - unresolved,
        "unresolved_count": unresolved,
        "target_warehouse_identity_verified": not unresolved,
        "products": views,
    }


def build_snapshot(
    catalog,
    warehouse_rows,
    *,
    merchdrop_warehouse_id=MERCHDROP_WAREHOUSE_ID,
    collected_at=None,
    now=None,
    max_age_seconds=600,
    fba_by_sku=None,
    target_product_map=None,
    target_inventory_bindings=None,
    captured_at_by_sku=None,
):
    target_coverage = (
        validate_target_inventory_coverage(catalog, target_product_map)
        if target_product_map is not None
        else None
    )
    if target_inventory_bindings is not None:
        if target_product_map is None:
            raise ValueError("Target bindings require the current Target product map")
        target_coverage = validate_target_inventory_bindings(
            catalog,
            target_product_map,
            target_inventory_bindings,
            warehouse_rows,
            merchdrop_warehouse_id,
        )
    now = utc(now or datetime.now(UTC))
    collected_at = utc(collected_at or now)
    age = (now - collected_at).total_seconds()
    if age < -60 or age > max_age_seconds:
        raise ValueError("stale or future inventory collection timestamp")
    by_sku = {}
    seen = set()
    for row in warehouse_rows:
        key = (row.get("sku"), row.get("warehouse_id"))
        if key in seen:
            raise ValueError("duplicate warehouse inventory row: {} / {}".format(*key))
        seen.add(key)
        by_sku.setdefault(row.get("sku"), []).append(row)
    products = []
    for item in catalog:
        sku = item.get("sku")
        stock = by_sku.get(sku, []) if sku else []
        primary = [r for r in stock if r.get("warehouse_id") == merchdrop_warehouse_id]
        if item.get("required", False) and not primary:
            raise ValueError(f"missing required Merchdrop SKU: {sku}")
        product = {
            "product": item["product"],
            "sku": sku,
            "warehouse_id": merchdrop_warehouse_id,
            "warehouse_stock": stock,
            "amazon_fba": {"status": "Not verified", "on_hand": None, "rows": []},
            "source_updated_at": None,
            "captured_at_utc": iso((captured_at_by_sku or {}).get(sku, collected_at)),
            "status": "Verified"
            if primary
            else ("SKU not mapped" if not sku else "Merchdrop row unavailable"),
        }
        for field in QUANTITIES:
            product[field] = None
        if captured_at_by_sku is not None and sku not in captured_at_by_sku:
            raise ValueError(f"missing per-SKU capture timestamp: {sku}")
        capture = utc(product["captured_at_utc"])
        if (now - capture).total_seconds() > max_age_seconds or capture > collected_at:
            raise ValueError(f"stale or future per-SKU capture timestamp: {sku}")
        if primary:
            row = primary[0]
            if (row.get("product") or {}).get("kit"):
                raise ValueError(f"inventory SKU is a kit, not a physical single: {sku}")
            for field in QUANTITIES:
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"missing or invalid {field} for {sku}")
                product[field] = value
            if not row.get("updated_at"):
                raise ValueError(f"missing source update timestamp for {sku}")
            product["source_updated_at"] = row["updated_at"]
        fba = (fba_by_sku or {}).get(sku)
        if fba:
            us_fba = [r for r in fba if r.get("marketplace_id") == "ATVPDKIKX0DER"]
            for record in us_fba:
                if not isinstance(record.get("quantity"), int):
                    raise ValueError(f"missing FBA quantity for {sku}")
            product["amazon_fba"] = {
                "status": "ShipHero Amazon US on-hand view only"
                if us_fba
                else "US marketplace not verified",
                "on_hand": sum(r["quantity"] for r in us_fba) if us_fba else None,
                "rows": fba,
            }
        products.append(product)
    if target_coverage and target_coverage.get("products"):
        by_product_sku = {p["sku"]: p for p in products}
        for view in target_coverage["products"]:
            verified = by_product_sku.get(view["inventory_sku"])
            view["captured_at_utc"] = verified["captured_at_utc"] if verified else None
            view["source_updated_at"] = verified["source_updated_at"] if verified else None
    return {
        "schema_version": "based.sop-inventory-snapshot.v1",
        "as_of_utc": iso(collected_at),
        "source": API_URL,
        "pacific_to_utc_hours": -collected_at.astimezone(ZoneInfo("America/Los_Angeles"))
        .utcoffset()
        .total_seconds()
        / 3600,
        "pool": {
            "warehouse_id": merchdrop_warehouse_id,
            "warehouse": "Merchdrop",
            "channels": ["Shopify", "TikTok", "Amazon FBM", "Target replenishment"],
            "excluded": [
                "stock already held at Target",
                "Amazon fulfillment inventory",
                "other warehouse locations",
            ],
            "available_rule": "Use ShipHero available as reported. Do not subtract Target retail sales or warehouse shipments again.",
            "target_retail_sellthrough_is_warehouse_depletion": False,
            "cover_scope": "Online merchant ordered units only until Target warehouse outbound is independently verified.",
        },
        "target_inventory_coverage": target_coverage,
        "target_inventory": (target_coverage or {}).get("products", []),
        "products": products,
    }


def snapshot_source_rows(snapshot):
    return [
        [
            p["sku"] or "",
            p["product"],
            p["warehouse_id"],
            p["on_hand"],
            p["available"],
            p["allocated"],
            p["backorder"],
            p.get("captured_at_utc", snapshot["as_of_utc"]),
        ]
        for p in snapshot["products"]
    ]


def cell(value):
    if value is None:
        return {}
    if isinstance(value, int | float) and not isinstance(value, bool):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value)}}


def sheet_requests(snapshot, sheet_id=CURRENT_INVENTORY_SHEET_ID):
    source_rows = snapshot_source_rows(snapshot)
    detail_rows = [
        [
            p["source_updated_at"],
            p["reserve_inventory"],
            p["sell_ahead"],
            p["amazon_fba"]["on_hand"],
            p["amazon_fba"]["status"],
            p["status"],
            snapshot["source"],
        ]
        for p in snapshot["products"]
    ]
    requests = []
    for start_column, rows in [(0, source_rows), (16, detail_rows)]:
        requests.append(
            {
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 7,
                        "endRowIndex": 7 + len(rows),
                        "startColumnIndex": start_column,
                        "endColumnIndex": start_column + len(rows[0]),
                    },
                    "rows": [{"values": [cell(v) for v in row]} for row in rows],
                    "fields": "userEnteredValue",
                }
            }
        )
    requests.append(
        {
            "updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": 1, "columnIndex": 16},
                "rows": [{"values": [cell(snapshot["pacific_to_utc_hours"])]}],
                "fields": "userEnteredValue",
            }
        }
    )
    requests.append(
        {
            "updateTable": {
                "table": {
                    "tableId": CURRENT_INVENTORY_TABLE_ID,
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 6,
                        "endRowIndex": 7 + len(source_rows),
                        "startColumnIndex": 0,
                        "endColumnIndex": 23,
                    },
                },
                "fields": "range",
            }
        }
    )
    coverage = snapshot.get("target_inventory_coverage") or {}
    notice = "Actual collection times are shown on each row."
    if "verified_count" in coverage and "unresolved_count" in coverage:
        notice += " Target retail mapping: %d verified, %d unverified." % (
            coverage["verified_count"],
            coverage["unresolved_count"],
        )
    requests.append(
        {
            "updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": 5, "columnIndex": 1},
                "rows": [{"values": [cell(notice)]}],
                "fields": "userEnteredValue",
            }
        }
    )
    return requests
