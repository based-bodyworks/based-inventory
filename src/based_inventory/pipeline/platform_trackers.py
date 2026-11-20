"""Mirror the frozen Slack workbook into dedicated, strictly owned tracker tabs.

Warehouse-only history, canonical formulas and stock-cover demand are untouched.
No sales collection or Slack publication happens in this module.
"""

from __future__ import annotations

import io
import math
from datetime import date

import openpyxl

from .google_api import DYNAMIC_ID, ERP_ID
from .models import canonical, digest

# Fixed identities are checked by both ID and title before every write.
TABS = tuple(
    (file, first_id + offset, title, source)
    for file, first_id in ((DYNAMIC_ID, 1740410295), (ERP_ID, 1740410298))
    for offset, (title, source) in enumerate(
        (
            ("Daily SKU Sales", "SKU Velocity"),
            ("Daily Sales Sources", "Sources"),
            ("Daily Sales Methods", "Methods"),
        )
    )
)
CURSOR = "platform-tracker-current"
LOCK_DAY = date(1970, 1, 2)
MAX_ROWS = 5000
MAX_COLUMNS = 10


def _normalize(rows):
    result = [["" if value is None else value for value in row] for row in rows]
    for row in result:
        while row and row[-1] in ("", None):
            row.pop()
    while result and not result[-1]:
        result.pop()
    return result


def _digest(rows):
    return digest(canonical(_normalize(rows)))


def build_views(package):
    """Use exact saved workbook cells, including n/a and source qualifications."""
    book = openpyxl.load_workbook(io.BytesIO(package.report), data_only=False, read_only=True)
    try:
        if set(book.sheetnames) != {"SKU Velocity", "Sources", "Methods"}:
            raise ValueError("Unexpected frozen sales workbook sheets")
        result = {}
        for name in book.sheetnames:
            sheet = book[name]
            if sheet.max_row + 5 > MAX_ROWS or sheet.max_column > MAX_COLUMNS:
                raise ValueError("Frozen sales workbook exceeds tracker view limits")
            rows = []
            for row in sheet:
                values = []
                for cell in row:
                    value = cell.value
                    if cell.data_type in {"f", "e"}:
                        raise ValueError("Frozen sales workbook contains a formula or error")
                    if value is not None and not isinstance(value, str | int | float | bool):
                        raise ValueError("Unexpected frozen sales cell type")
                    if isinstance(value, float) and not math.isfinite(value):
                        raise ValueError("Invalid frozen sales number")
                    values.append(value)
                rows.append(values)
            result[name] = _normalize(
                [
                    ["BASED saved daily sales report"],
                    ["Reporting date", str(package.day), "Report version", package.version_id],
                    ["Workbook SHA-256", digest(package.report)],
                    [
                        "Scope",
                        "Exact saved Slack workbook values. Warehouse stock cover uses its separate warehouse source.",
                    ],
                    [],
                    *rows,
                ]
            )
        return result
    finally:
        book.close()


def _plan(storage, package):
    key = f"platform-tracker-plan:{package.day}:{package.version_id}"
    candidate = {
        "day": str(package.day),
        "version_id": package.version_id,
        "views": build_views(package),
    }
    saved = storage._read(key)
    if saved is not None and saved != candidate:
        raise ValueError("Frozen platform tracker plan changed")
    return key, candidate


def publish_views(storage, package, google):
    """Reconcile partial writes under one cross-date lock without hiding edits."""
    with storage.lock(LOCK_DAY):
        key, plan = _plan(storage, package)
        cursor = storage._read(CURSOR)
        if cursor and date.fromisoformat(cursor["day"]) > package.day:
            raise ValueError("A later report owns the platform tracker views")
        if (
            cursor
            and cursor["day"] == str(package.day)
            and cursor["version_id"] != package.version_id
        ):
            raise ValueError("A different report already owns this tracker date")
        previous = dict((cursor or {}).get("destinations", {}))
        current = {}
        # Read every destination before creating a tab or changing any value.
        for file, tab_id, title, source in TABS:
            identity = f"{file}:{tab_id}"
            actual = google.platform_read(file, tab_id, title)
            wanted = plan["views"][source]
            actual_hash = _digest(actual or [])
            prior_hash = previous.get(identity)
            if actual is None:
                if prior_hash and prior_hash != _digest([]):
                    raise ValueError("A previously verified platform tab is missing")
            elif actual_hash not in {_digest(wanted), prior_hash}:
                raise ValueError("Platform sales changed outside the saved publication plan")
            current[identity] = actual_hash
        storage._write(key, plan, once=True)
        if storage._read(key) != plan:
            raise ValueError("Platform tracker plan was not saved")
        # Persist the date fence and exact intent before the first provider write.
        state = {
            "day": str(package.day),
            "version_id": package.version_id,
            "status": "pending",
            "destinations": previous,
        }
        for identity, actual_hash in current.items():
            state["destinations"].setdefault(identity, actual_hash)
        storage._write(CURSOR, state)
        for file, tab_id, title, source in TABS:
            identity = f"{file}:{tab_id}"
            wanted = plan["views"][source]
            expected_hash = _digest(wanted)
            if current[identity] != expected_hash:
                before = google.platform_read(file, tab_id, title)
                if _digest(before or []) != current[identity]:
                    raise ValueError("Platform sales changed outside the saved publication plan")
                google.platform_write(file, tab_id, title, wanted)
            actual = google.platform_read(file, tab_id, title)
            if actual is None or _digest(actual) != expected_hash:
                raise ValueError("Native platform tracker readback differs from frozen report")
            state["destinations"][identity] = expected_hash
            storage._write(CURSOR, state)
        state["status"] = "verified"
        storage._write(CURSOR, state)
        verify_views(storage, package, google)


def verify_views(storage, package, google):
    """Read-only provider verification, independent of success receipts."""
    key, plan = _plan(storage, package)
    if storage._read(key) != plan:
        raise ValueError("Platform tracker saved plan is missing or differs")
    state = storage._read(CURSOR)
    if not state or any(
        state.get(k) != v
        for k, v in {
            "day": str(package.day),
            "version_id": package.version_id,
            "status": "verified",
        }.items()
    ):
        raise ValueError("Platform tracker version is missing or differs")
    for file, tab_id, title, source in TABS:
        actual = google.platform_read(file, tab_id, title)
        expected_hash = _digest(plan["views"][source])
        if (
            actual is None
            or _digest(actual) != expected_hash
            or state["destinations"].get(f"{file}:{tab_id}") != expected_hash
        ):
            raise ValueError("Native platform tracker readback differs from frozen report")


def report_inputs(storage, day, slack):
    from based_inventory.all_channel_sales import decode_source_proof
    from based_inventory.sales_report_archive import SalesReportArchive

    archive = SalesReportArchive(storage)
    package = archive.latest_package(day)
    if package is None:
        raise ValueError("Today's saved all-channel report is missing")
    if len(package.history_ids) != 7:
        raise ValueError("Seven saved native sales dates are required for tracker cutover")
    receipt = (
        archive.delivery_store(package, slack.channel).receipts(package.version_id).get("slack", {})
    )
    if receipt.get("status") != "verified":
        raise ValueError("Native sales report delivery is not verified")
    slack.verify(receipt["provider_id"], package)
    source = archive.latest_day(day)
    parsed = decode_source_proof(source.sources["Target"])["parsed"]
    if (
        parsed.get("date") != str(day)
        or parsed.get("safe_to_write") is not True
        or parsed.get("unmapped_products")
    ):
        raise ValueError("Saved Target source is not safe for tracker publication")
    return package, parsed


def check_or_publish(location, day, google, slack, *, publish=False):
    """Join the frozen native report and its views, without collecting or sending."""
    from based_inventory.velocity_archive import VelocityArchive

    from .runner import require_writes

    if publish:
        require_writes()
    with VelocityArchive(location, read_only=not publish) as storage:
        package, _ = report_inputs(storage, day, slack)
        if publish:
            publish_views(storage, package, google)
        else:
            verify_views(storage, package, google)
        return package.version_id
