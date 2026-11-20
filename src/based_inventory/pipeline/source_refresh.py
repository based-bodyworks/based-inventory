"""Refresh separate retail, AFN and warehouse sources from server credentials."""

from __future__ import annotations

import json
import os
from datetime import date
from hashlib import sha256

from .amazon_refresh import PolarAPI, collect_amazon, publish_amazon
from .inventory_refresh import collect_inventory, inventory_cover_values, publish_inventory
from .target_refresh import SourceIncomplete, collect_target, publish_target
from .workbook_archive import archive_target_workbook, metadata


def require_mode(mode):
    if mode not in {"compare", "publish"}:
        raise ValueError("Unknown source refresh mode")
    if mode == "publish" and os.getenv("PIPELINE_ALLOW_WRITES") != "1":
        raise PermissionError("Cloud source writes are disabled")


def refresh_sources(
    day: date,
    store,
    version_id: str,
    google,
    *,
    mode: str,
    polar_token: str,
    target_query: str,
    target_workbook_id: str,
) -> dict:
    require_mode(mode)
    version = store.load(version_id)
    if version.day != day:
        raise ValueError("Stored merchant version does not match source reporting day")
    store.assert_current(version_id)
    # Authentication and stock reads reuse the existing server client, never a Mac .env.
    from based_inventory.shiphero import ShipHeroClient
    from based_inventory.shiphero_auth import resolve_access_token

    result = {"version_id": version_id, "mode": mode, "sources": {}}

    def run(name, action):
        try:
            store.assert_current(version_id)
            if mode == "publish":
                store.assert_destination_current(version_id, name)
            saved = store.get_plan(version_id, name) if mode == "publish" else None

            def freeze(verification):
                store.assert_destination_current(version_id, name)
                intended = {"status": "verified", "verification": verification}
                digest = sha256(
                    json.dumps(intended, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                candidate = {"digest": digest, "receipt": intended}
                if store.plan_once(version_id, name, candidate) != candidate:
                    raise SourceIncomplete(
                        "A different source capture already belongs to this merchant version"
                    )

            if saved:
                existing = store.receipts(version_id).get(name, {})
                if (
                    existing.get("status") == "verified"
                    and existing.get("digest") == saved["digest"]
                ):
                    verify_source_plan(google, saved)
                else:
                    replay_source_plan(google, saved)
                receipt = saved["receipt"]
            else:
                receipt = action(freeze if mode == "publish" else None)
                if mode == "publish":
                    saved = store.get_plan(version_id, name)
                    if not saved:
                        raise SourceIncomplete("Source write intent was not persisted")
                    verify_source_plan(google, saved)
                    receipt = saved["receipt"]
            if receipt.get("status") != "verified":
                raise SourceIncomplete("Source readback did not verify")
            if mode == "publish":
                store.record_receipt(
                    version_id, name, saved["digest"], receipt["verification"].get("file_id")
                )
            result["sources"][name] = receipt
        except (ValueError, RuntimeError, PermissionError, KeyError, TypeError):
            # Surface known safe validation messages only; raw provider payloads are never logged.
            import sys

            error = sys.exception()
            message = (
                str(error)
                if isinstance(error, SourceIncomplete | ValueError)
                else f"{name} source validation failed ({type(error).__name__})"
            )
            result["sources"][name] = {"status": "incomplete", "reason": message}

    target = None

    def target_action(before_write):
        nonlocal target
        target = collect_target(day, google, target_query)
        return publish_target(target, google, mode=mode, before_write=before_write)

    run("target", target_action)
    if target is None and result["sources"]["target"].get("status") == "verified":
        target = result["sources"]["target"]["verification"]["target"]
    shiphero = None

    def get_stock_client():
        nonlocal shiphero
        if shiphero is None:
            token = resolve_access_token(
                os.getenv("SHIPHERO_REFRESH_TOKEN"), os.getenv("SHIPHERO_ACCESS_TOKEN")
            )
            shiphero = ShipHeroClient(token)
        return shiphero

    run(
        "amazon",
        lambda before_write: publish_amazon(
            collect_amazon(day, PolarAPI(polar_token), get_stock_client()),
            google,
            mode=mode,
            before_write=before_write,
        ),
    )
    run(
        "inventory",
        lambda before_write: publish_inventory(
            collect_inventory(get_stock_client()), google, mode=mode, before_write=before_write
        ),
    )

    def workbook_action(before_write):
        metadata(google, target_workbook_id)
        if target is None:
            raise SourceIncomplete("Cloud Target workbook requires a complete parsed retail report")
        return archive_target_workbook(
            google, target_workbook_id, target, mode=mode, before_write=before_write
        )

    run("workbook", workbook_action)
    result["status"] = (
        "verified"
        if all(r["status"] == "verified" for r in result["sources"].values())
        else "incomplete"
    )
    return result


def verify_source_plan(google, plan):
    """Check saved destination content without re-querying changing providers."""
    from .cloud_helpers.verify_target_readback import verify
    from .google_api import DYNAMIC_ID, ERP_ID
    from .target_refresh import verify_requests

    verification = plan["receipt"]["verification"]
    if not any(verification.get("requests", {}).values()) and "file_id" not in verification:
        raise SourceIncomplete("Saved source plan contains no native verification scope")
    for spreadsheet, requests in verification.get("requests", {}).items():
        verify_requests(google, spreadsheet, requests)
    if "target" in verification:
        parsed = verification["target"]
        tabs = {
            s["properties"]["sheetId"]: s["properties"]["title"]
            for s in google.metadata(ERP_ID).get("sheets", [])
        }
        a1 = "'" + tabs[560223170].replace("'", "''") + "'"
        native = google.cells(ERP_ID, [a1 + "!A:L"])
        native["spreadsheetId"] = ERP_ID
        verify(parsed, native)
        actual = [
            r
            for r in google.values(DYNAMIC_ID, verification["dynamic_a1"] + "!M:Q")
            if r and r[0] == parsed["sheets_date_serial"]
        ]
        if sorted(actual, key=lambda r: r[3]) != sorted(
            verification["dynamic_expected"], key=lambda r: r[3]
        ):
            raise SourceIncomplete("Saved Target projection changed")
    if "inventory_formulas" in verification or "inventory_cover_values" in verification:
        native = google.cells(ERP_ID, [verification["inventory_range"]])
        if "inventory_cover_values" in verification:
            if inventory_cover_values(native) != verification["inventory_cover_values"]:
                raise SourceIncomplete("Saved warehouse stock-cover cells changed")
        else:
            formulas = [
                [c.get("userEnteredValue", {}).get("formulaValue") for c in r.get("values", [])]
                for s in native.get("sheets", [])
                for g in s.get("data", [])
                for r in g.get("rowData", [])
            ]
            if formulas != verification["inventory_formulas"]:
                raise SourceIncomplete("Saved warehouse stock-cover formulas changed")
    if "file_id" in verification:
        metadata(google, verification["file_id"])
        content = google.request(
            "GET",
            f"https://www.googleapis.com/drive/v3/files/{verification['file_id']}",
            params={"alt": "media", "supportsAllDrives": "true"},
        ).content
        if sha256(content).hexdigest() != verification["file_digest"]:
            raise SourceIncomplete("Saved cloud Target workbook content changed")
    receipt = plan["receipt"]
    digest = (
        receipt.get("digest")
        or sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    if digest != plan["digest"]:
        raise SourceIncomplete("Saved source plan digest does not match receipt")
    return True


def verify_sources(store, version_id, google):
    """Read-only monitor checks against the actual morning capture."""
    receipts = store.receipts(version_id)
    results = {}
    for name in ("target", "amazon", "inventory", "workbook"):
        plan = store.get_plan(version_id, name)
        receipt = receipts.get(name, {})
        try:
            if (
                not plan
                or receipt.get("status") != "verified"
                or receipt.get("digest") != plan.get("digest")
            ):
                raise SourceIncomplete("Saved source publication is incomplete")
            results[name] = verify_source_plan(google, plan)
        except (ValueError, RuntimeError, KeyError, TypeError):
            results[name] = False
    return results


def replay_source_plan(google, plan):
    """Reconcile a pending frozen capture after a process or response loss."""
    import base64
    import contextlib

    from .google_api import GoogleAPIError
    from .workbook_archive import XLSX

    require_mode("publish")
    receipt = plan["receipt"]
    digest = (
        receipt.get("digest")
        or sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    if digest != plan.get("digest"):
        raise SourceIncomplete("Frozen source intent digest is invalid; replay refused")
    verification = plan["receipt"]["verification"]
    for spreadsheet, requests in verification.get("requests", {}).items():
        if requests:
            with contextlib.suppress(GoogleAPIError):
                google.batch(spreadsheet, requests)
    if "file_id" in verification:
        file_id = verification["file_id"]
        metadata(google, file_id)
        current = google.request(
            "GET",
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
        ).content
        digest = sha256(current).hexdigest()
        if digest not in {verification["original_digest"], verification["file_digest"]}:
            raise SourceIncomplete(
                "Cloud Target workbook changed after frozen write intent; manual reconciliation required"
            )
        if digest != verification["file_digest"]:
            frozen = base64.b64decode(verification["updated_base64"], validate=True)
            if sha256(frozen).hexdigest() != verification["file_digest"]:
                raise SourceIncomplete("Frozen workbook bytes do not match intended hash")
            with contextlib.suppress(GoogleAPIError):
                google.request(
                    "PATCH",
                    f"https://www.googleapis.com/upload/drive/v3/files/{file_id}",
                    params={"uploadType": "media", "supportsAllDrives": "true"},
                    headers={"Content-Type": XLSX},
                    data=frozen,
                )
    return verify_source_plan(google, plan)
