import base64
from datetime import UTC, date

import pytest

from based_inventory.pipeline.source_refresh import refresh_sources
from based_inventory.pipeline.target_refresh import SourceIncomplete, collect_target
from based_inventory.pipeline.workbook_archive import verify_destination


class Mail:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def json(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if url.endswith("/messages"):
            return {"messages": [{"id": k} for k in self.messages]}
        return self.messages[url.rsplit("/", 1)[1]]


def mail(text, ident="one"):
    return {
        "id": ident,
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        },
    }


def test_compare_never_enables_writes(monkeypatch):
    monkeypatch.setenv("PIPELINE_ALLOW_WRITES", "1")
    with pytest.raises(ValueError, match="mode"):
        refresh_sources(
            date(2026, 11, 16),
            None,
            "x",
            None,
            mode="oops",
            polar_token="",
            target_query="",
            target_workbook_id="",
        )


def test_publish_guard_precedes_source_reads(monkeypatch):
    monkeypatch.delenv("PIPELINE_ALLOW_WRITES", raising=False)
    with pytest.raises(PermissionError):
        refresh_sources(
            date(2026, 11, 16),
            None,
            "x",
            None,
            mode="publish",
            polar_token="",
            target_query="",
            target_workbook_id="",
        )


def test_wrong_destination_blocks():
    with pytest.raises(ValueError, match="Marketing Operations"):
        verify_destination({"driveId": "personal", "parents": ["other"]})


def test_mail_feed_pagination_not_silently_dropped():
    class Paged(Mail):
        def json(self, *a, **kw):
            return {"messages": [], "nextPageToken": "more"}

    with pytest.raises(SourceIncomplete):
        collect_target(date(2026, 11, 15), Paged({}), "from:target")


def test_duplicate_matching_reports_require_review():
    from pathlib import Path

    fixture = (
        Path(__file__).parents[1]
        / "src/based_inventory/pipeline/cloud_helpers/fixtures/target_oct28_2026.txt"
    )
    m = Mail(
        {
            "one": mail(fixture.read_text()),
            "two": mail(fixture.read_text(), "two"),
            "prior": mail(fixture.with_name("target_oct27_2026.txt").read_text(), "prior"),
        }
    )
    with pytest.raises(SourceIncomplete, match="multiple"):
        collect_target(date(2026, 10, 28), m, "from:target")


def test_original_target_fixture_collected_independently():
    from pathlib import Path

    folder = Path(__file__).parents[1] / "src/based_inventory/pipeline/cloud_helpers/fixtures"
    google = Mail(
        {
            "one": mail((folder / "target_oct28_2026.txt").read_text()),
            "prior": mail((folder / "target_oct27_2026.txt").read_text(), "prior"),
        }
    )
    result = collect_target(date(2026, 10, 28), google, "verified sender")
    assert result["safe_to_write"] and len(result["products"]) == 9
    assert result["derived_daily_units"] == 5453
    assert all(method == "GET" for method, _, _ in google.calls)


def test_sunday_does_not_require_prior_mail():
    from pathlib import Path

    text = (
        (
            Path(__file__).parents[1]
            / "src/based_inventory/pipeline/cloud_helpers/fixtures/target_oct28_2026.txt"
        )
        .read_text()
        .replace("10.28", "11.15")
    )
    result = collect_target(date(2026, 11, 15), Mail({"one": mail(text)}), "verified sender")
    assert result["previous_message_id"] is None
    assert result["derived_daily_units"] == result["grand"]["week_units"]


def test_provider_boundary_rejects_pacific_and_missing_date():
    from based_inventory.pipeline.amazon_refresh import validate_utc_intervals

    result = {
        "truncated": False,
        "row_count": 1,
        "rows": [
            {
                "marketplace_id": "ATVPDKIKX0DER",
                "interval_start": "2026-11-15T00:00:00-08:00",
                "interval_end": "2026-11-16T00:00:00-08:00",
            }
        ],
    }
    with pytest.raises(SourceIncomplete, match="UTC"):
        validate_utc_intervals(result, date(2026, 11, 15), date(2026, 11, 15))
    result["rows"][0].update(
        interval_start="2026-11-15T00:00:00Z", interval_end="2026-11-16T00:00:00Z"
    )
    validate_utc_intervals(result, date(2026, 11, 15), date(2026, 11, 15))
    with pytest.raises(SourceIncomplete, match="incomplete"):
        validate_utc_intervals(result, date(2026, 11, 14), date(2026, 11, 15))


def test_polar_api_redacts_auth_and_retries():
    from unittest.mock import Mock

    from based_inventory.pipeline.amazon_refresh import PolarAPI

    session = Mock()
    session.post.side_effect = [
        Mock(status_code=503),
        Mock(status_code=200, json=lambda: {"conversation_id": "fake"}),
    ]
    waits = []
    api = PolarAPI("fake-secret", session=session, sleep=waits.append)
    assert api.call("get_context", {}) == {"conversation_id": "fake"} and waits == [1]
    session.post.return_value = Mock(status_code=401)
    session.post.side_effect = None
    with pytest.raises(SourceIncomplete, match="HTTP 401") as error:
        api.call("get_context", {})
    assert "fake-secret" not in str(error.value)


def test_workbook_constant_patch_preserves_formulas_caches_other_parts():
    from io import BytesIO
    from zipfile import ZipFile

    from openpyxl import Workbook

    from based_inventory.pipeline.workbook_archive import patch_cells

    book = Workbook()
    book.active.title = "Daily Data"
    book.active["A1"] = 1
    book.active["B1"] = "=A1+1"
    buf = BytesIO()
    book.save(buf)
    before = buf.getvalue()
    after = patch_cells(before, "Daily Data", {"A1": 12})
    with ZipFile(BytesIO(before)) as first, ZipFile(BytesIO(after)) as second:
        assert first.namelist() == second.namelist()
        assert all(
            first.read(name) == second.read(name)
            for name in first.namelist()
            if name != "xl/worksheets/sheet1.xml"
        )
        assert b"<f>A1+1</f>" in second.read("xl/worksheets/sheet1.xml")
    with pytest.raises(ValueError, match="formula"):
        patch_cells(before, "Daily Data", {"B1": 0})


def test_failed_readback_never_records_source_receipts(monkeypatch):
    from unittest.mock import Mock

    import based_inventory.pipeline.source_refresh as module

    store = Mock(unsafe=True)
    store.get_plan.return_value = None
    store.load.return_value = Mock(day=date(2026, 11, 16))
    monkeypatch.setenv("PIPELINE_ALLOW_WRITES", "1")
    monkeypatch.setattr(
        module, "collect_target", Mock(side_effect=SourceIncomplete("Missing dated Target report"))
    )
    monkeypatch.setattr(
        module, "collect_amazon", Mock(side_effect=SourceIncomplete("Truncated Amazon report"))
    )
    monkeypatch.setattr(
        module, "collect_inventory", Mock(side_effect=SourceIncomplete("Stock source incomplete"))
    )
    monkeypatch.setattr(module, "metadata", Mock(side_effect=ValueError("File not configured")))
    monkeypatch.setattr("based_inventory.shiphero_auth.resolve_access_token", lambda *_: "fake")
    result = module.refresh_sources(
        date(2026, 11, 16),
        store,
        "version",
        Mock(),
        mode="publish",
        polar_token="fake",
        target_query="verified sender",
        target_workbook_id="file",
    )
    assert result["status"] == "incomplete"
    assert set(result["sources"]) == {"target", "amazon", "inventory", "workbook"}
    store.record_receipt.assert_not_called()


def test_cloud_workbook_updates_daily_summary_chart_caches_without_excel():
    from io import BytesIO
    from pathlib import Path
    from zipfile import ZipFile

    from openpyxl import load_workbook

    from based_inventory.pipeline.workbook_archive import target_workbook_update

    path = (
        Path(__file__).parents[1]
        / "src/based_inventory/pipeline/cloud_helpers/fixtures/synthetic_target_template.xlsx"
    )
    parsed = {
        "date": "2026-10-28",
        "sheets_date_serial": 46323,
        "message_id": "synthetic-current",
        "previous_message_id": "synthetic-prior",
        "reported_daily_units": 5,
        "derived_daily_units": 5,
        "derived_daily_retail": 50,
        "units_variance": 0,
        "grand": {"week_forecast_units": 30},
        "products": [
            {
                "product": "Sea Salt Spray",
                "sku": "synthetic",
                "daily_units": 5,
                "daily_retail": 50,
                "week_units": 15,
                "week_retail": 150,
                "week_forecast_units": 30,
                "out_of_stock": 0.01,
            }
        ],
        "workbook_summary": {
            "asp": 10,
            "launch_units": 15,
            "stores": 1,
            "overall_oos": 0.01,
            "received_serial": 46324,
        },
    }
    before = path.read_bytes()
    after = target_workbook_update(before, parsed)
    native = load_workbook(BytesIO(after), data_only=True)
    assert native["Daily Data"]["F6"].value == 5
    assert native["Daily Data"]["E6"].value == 50
    assert native["Daily Data"]["G6"].value == 10
    assert native["Summary"]["C7"].value == 15
    assert native["Summary"]["B16"].value == 150
    assert native["Summary"]["B36"].value == 2
    with ZipFile(BytesIO(before)) as first, ZipFile(BytesIO(after)) as second:
        for name in first.namelist():
            if name not in {
                "xl/worksheets/sheet1.xml",
                "xl/worksheets/sheet2.xml",
                "xl/charts/chart1.xml",
            }:
                assert first.read(name) == second.read(name)
        assert b"<c:v>150</c:v>" in second.read("xl/charts/chart1.xml")
    formulas = load_workbook(BytesIO(after), data_only=False)
    assert formulas["Daily Data"]["F6"].value.startswith("=IF(")
    assert formulas["Summary"]["A7"].value == "=SUM('Daily Data'!$E$5:$E$500)"


class NativeSheets:
    def __init__(self):
        self.data = {}
        self.tables = {}
        self.writes = []

    def metadata(self, _):
        return {"sheets": [{"properties": {"sheetId": 1740410290, "title": "Current Inventory"}}]}

    def json(self, *_, **__):
        return {"sheets": [{"tables": list(self.tables.values())}]}

    def batch(self, _, requests):
        from copy import deepcopy

        self.writes.extend(deepcopy(requests))
        for req in requests:
            if "updateTable" in req:
                table = req["updateTable"]["table"]
                self.tables[table["tableId"]] = deepcopy(table)
                continue
            upd = req["updateCells"]
            loc = upd.get("range", upd.get("start"))
            row = loc.get("startRowIndex", loc.get("rowIndex", 0))
            col = loc.get("startColumnIndex", loc.get("columnIndex", 0))
            endrow = loc.get("endRowIndex", row + len(upd["rows"]))
            endcol = loc.get("endColumnIndex", col + max(len(r["values"]) for r in upd["rows"]))
            for r in range(endrow - row):
                vals = upd["rows"][r]["values"] if r < len(upd["rows"]) else []
                for c in range(endcol - col):
                    val = vals[c] if c < len(vals) else {}
                    dest = self.data.setdefault((row + r, col + c), {})
                    if "userEnteredValue" in upd["fields"]:
                        dest["userEnteredValue"] = val.get("userEnteredValue", {})
                        dest["effectiveValue"] = val.get("userEnteredValue", {})
                    if "note" in upd["fields"]:
                        dest["note"] = val.get("note", "")

    def cells(self, _, ranges):
        from openpyxl.utils.cell import range_boundaries

        c1, r1, c2, r2 = range_boundaries(ranges[0].split("!")[1])
        return {
            "sheets": [
                {
                    "properties": {"sheetId": 1740410290},
                    "data": [
                        {
                            "startRow": r1 - 1,
                            "startColumn": c1 - 1,
                            "rowData": [
                                {"values": [self.data.get((r, c), {}) for c in range(c1 - 1, c2)]}
                                for r in range(r1 - 1, r2)
                            ],
                        }
                    ],
                }
            ]
        }

    def values(self, spreadsheet, a1):
        native = self.cells(spreadsheet, [a1])
        return [
            [next(iter(c.get("effectiveValue", {}).values()), None) for c in r["values"]]
            for r in native["sheets"][0]["data"][0]["rowData"]
        ]


def test_zero_available_stock_is_published_without_changing_cover_formulas(monkeypatch):
    from datetime import datetime

    from based_inventory.pipeline.cloud_helpers.collect_inventory_snapshot import build_snapshot
    from based_inventory.pipeline.inventory_refresh import publish_inventory

    now = datetime(2026, 11, 17, tzinfo=UTC)
    source = {
        "sku": "physical",
        "warehouse_id": "warehouse",
        "warehouse_identifier": "main",
        "on_hand": 7,
        "available": 0,
        "allocated": 7,
        "reserve_inventory": 0,
        "backorder": 0,
        "sell_ahead": 0,
        "updated_at": now.isoformat(),
        "product": {"name": "Example", "kit": False},
    }
    snapshot = build_snapshot(
        [{"product": "Example", "sku": "physical", "required": True}],
        [source],
        merchdrop_warehouse_id="warehouse",
        collected_at=now,
        now=now,
    )
    google = NativeSheets()
    for col in range(8, 16):
        google.data[(7, col)] = {
            "userEnteredValue": {"formulaValue": "=1"},
            "effectiveValue": {"numberValue": 1},
        }
    google.data[(7, 4)] = {"note": "[based.inventory.snapshot] Old snapshot says 60 available"}
    google.data[(7, 3)] = {"note": "Owner annotation: confirm packaging"}
    monkeypatch.setenv("PIPELINE_ALLOW_WRITES", "1")
    result = publish_inventory(snapshot, google, mode="publish")
    assert result["status"] == "verified"
    assert google.data[(7, 4)]["effectiveValue"] == {"numberValue": 0}
    assert "Old snapshot" not in google.data[(7, 4)]["note"]
    assert google.data[(7, 3)]["note"] == "Owner annotation: confirm packaging"
    assert all(
        google.data[(7, col)]["userEnteredValue"] == {"formulaValue": "=1"} for col in range(8, 16)
    )
    count = len(google.writes)
    publish_inventory(snapshot, google, mode="compare")
    assert len(google.writes) == count


def test_missing_formula_or_wrong_stock_readback_cannot_verify(monkeypatch):
    from based_inventory.pipeline.target_refresh import verify_requests

    google = NativeSheets()
    request = {
        "updateCells": {
            "start": {"sheetId": 1740410290, "rowIndex": 7, "columnIndex": 4},
            "rows": [{"values": [{"userEnteredValue": {"numberValue": 7}}]}],
            "fields": "userEnteredValue",
        }
    }
    with pytest.raises(SourceIncomplete, match="cell differs"):
        verify_requests(google, "spreadsheet", [request])


def test_unknown_workbook_summary_formula_blocks_refresh():
    from io import BytesIO
    from pathlib import Path

    from openpyxl import load_workbook

    from based_inventory.pipeline.workbook_archive import refresh_summary_caches

    path = (
        Path(__file__).parents[1]
        / "src/based_inventory/pipeline/cloud_helpers/fixtures/synthetic_target_template.xlsx"
    )
    book = load_workbook(BytesIO(path.read_bytes()))
    book["Summary"]["A7"] = "=UNKNOWN()"
    out = BytesIO()
    book.save(out)
    with pytest.raises(ValueError, match="Unsupported.*formula"):
        refresh_summary_caches(out.getvalue())


def test_target_summary_preserves_merchant_values_and_version():
    from based_inventory.pipeline.merchant_publish import DAILY_HEADER
    from based_inventory.pipeline.target_refresh import target_summary_plan

    class Summary:
        def metadata(self, _):
            return {"sheets": [{"properties": {"sheetId": 1275479413, "title": "Daily Sales"}}]}

        def values(self, *_):
            return [
                DAILY_HEADER,
                [
                    46323,
                    10,
                    20,
                    30,
                    60,
                    0,
                    0,
                    "merchant-url",
                    "old-target",
                    "Target stale. Merchant verification: Version saved-123.",
                ],
            ]

    parsed = {
        "sheets_date_serial": 46323,
        "reported_daily_units": 100,
        "derived_daily_units": 101,
        "derived_daily_retail": 500,
        "units_variance": 1,
        "message_id": "new-target",
    }
    requests, _, _, old = target_summary_plan(parsed, Summary())
    assert [r["updateCells"]["start"]["columnIndex"] for r in requests] == [5, 8]
    note = requests[1]["updateCells"]["rows"][0]["values"][1]["userEnteredValue"]["stringValue"]
    assert note.endswith(" Merchant verification: Version saved-123.")
    assert "variance +1" in note
    assert old[1:5] == [10, 20, 30, 60]


def test_vetted_source_helper_regressions_are_part_of_cloud_checks():
    import os
    import subprocess
    import sys
    from pathlib import Path

    folder = Path(__file__).parents[1] / "src/based_inventory/pipeline/cloud_helpers"
    env = dict(os.environ, PYTHONPATH=str(folder) + os.pathsep + str(folder.parents[2]))
    for tests in (folder, folder / "tests"):
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-p", "test_*.py"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_dynamic_target_only_appends_to_empty_projection_and_reuses_exact_day():
    from unittest.mock import Mock

    from based_inventory.pipeline.target_refresh import target_dynamic_plan

    parsed = {
        "sheets_date_serial": 46323,
        "products": [{"product": "Example", "sku": "single", "daily_units": 7}],
    }
    google = Mock()
    google.metadata.return_value = {
        "sheets": [
            {
                "properties": {
                    "sheetId": 708272086,
                    "title": "Target",
                    "gridProperties": {"rowCount": 999},
                }
            }
        ]
    }
    google.values.return_value = [
        ["Date", "Name", "Qty", "SKU", "Store"],
        [46322, "Example", 3, "single", "TARGET"],
    ]
    google.cells.return_value = {"sheets": [{"data": [{"rowData": [{"values": [{}] * 5}]}]}]}
    requests, _, expected = target_dynamic_plan(parsed, google)
    update = requests[0]["updateCells"]
    assert update["start"] == {"sheetId": 708272086, "rowIndex": 2, "columnIndex": 12}
    assert len(update["rows"][0]["values"]) == 5
    google.values.return_value.append(expected[0])
    assert target_dynamic_plan(parsed, google)[0] == []
    google.values.return_value.pop()
    google.cells.return_value = {
        "sheets": [
            {
                "data": [
                    {"rowData": [{"values": [{"userEnteredValue": {"formulaValue": "=SPILL()"}}]}]}
                ]
            }
        ]
    }
    with pytest.raises(SourceIncomplete, match="formulas"):
        target_dynamic_plan(parsed, google)


def test_server_afn_query_uses_complete_utc_scope_and_independent_total(monkeypatch):
    from datetime import timedelta
    from unittest.mock import Mock

    import based_inventory.pipeline.amazon_refresh as module

    start = date(2026, 11, 10)
    end = date(2026, 11, 16)
    rows = [
        {"date": (start + timedelta(days=i)).isoformat(), "sku": "single", module.METRIC: 1}
        for i in range(7)
    ]

    class Polar:
        conversation = "fake"

        def __init__(self):
            self.queries = []

        def context(self):
            return {}

        def call(self, name, args):
            self.queries.append((name, args))
            if name == "list_dimensions":
                return {
                    "dimensions": ["sku"],
                    "filters": [
                        "fulfillment_channel",
                        "marketplace_id",
                        "sales_channel",
                        "fulfillment_status",
                    ],
                }
            return {
                "tableData": rows,
                "totalData": [{module.METRIC: 7}],
                "deepLink": "https://app.polaranalytics.com/fake",
            }

        def live(self, name, args):
            if name == "describe":
                return {"rows": [{"operation": "sales"}, {"operation": "inventory"}]}
            if name == "inventory":
                return {"fetched_at": "2026-11-17T00:00:00Z"}
            return {
                "truncated": False,
                "row_count": 7,
                "rows": [
                    {
                        "marketplace_id": module.MARKETPLACE,
                        "interval_start": (start + timedelta(days=i)).isoformat() + "T00:00:00Z",
                        "interval_end": (start + timedelta(days=i + 1)).isoformat() + "T00:00:00Z",
                    }
                    for i in range(7)
                ],
            }

    monkeypatch.setattr(module, "asset", lambda _: [{"product": "Example", "skus": ["single"]}])
    polar = Polar()
    shiphero = Mock()
    shiphero.fetch_all_kits.return_value = []
    result = module.collect_amazon(end, polar, shiphero)
    assert result["normalized"]["physical_units"] == 7
    reports = [args for name, args in polar.queries if name == "generate_report"]
    assert len(reports) == 2 and reports[1]["granularity"] == "none"
    import json

    assert json.loads(reports[0]["settings"])["timezone"] == "UTC"
    rules = json.loads(reports[0]["rules"])
    assert rules["fulfillment_status"] == [{"operator": "ISNOT", "value": ["Canceled"]}]
    assert rules["fulfillment_channel"] == [{"operator": "IS", "value": ["AFN"]}]


def test_source_monitor_checks_saved_capture_without_calling_sources(monkeypatch):
    import json
    from hashlib import sha256
    from unittest.mock import Mock

    import based_inventory.pipeline.source_refresh as module

    receipt = {"status": "verified", "verification": {"requests": {}}}
    digest = sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    store = Mock(unsafe=True)
    store.get_plan.return_value = {"receipt": receipt, "digest": digest}
    store.receipts.return_value = {
        name: {"status": "verified", "digest": digest}
        for name in ("target", "amazon", "inventory", "workbook")
    }
    monkeypatch.setattr(
        module,
        "collect_inventory",
        Mock(side_effect=AssertionError("must not collect fresh stock")),
    )
    monkeypatch.setattr(
        module, "collect_amazon", Mock(side_effect=AssertionError("must not pull AFN"))
    )
    monkeypatch.setattr(module, "verify_source_plan", Mock(return_value=True))
    assert module.verify_sources(store, "version", Mock()) == dict.fromkeys(
        ("target", "amazon", "inventory", "workbook"), True
    )
    store.plan_once.assert_not_called()
    store.receipts.return_value["inventory"]["digest"] = "changed"
    assert module.verify_sources(store, "version", Mock())["inventory"] is False


def test_crash_after_stock_write_replays_frozen_capture_without_provider_pull(monkeypatch):
    import json
    from datetime import datetime
    from hashlib import sha256

    from based_inventory.pipeline.cloud_helpers.collect_inventory_snapshot import build_snapshot
    from based_inventory.pipeline.inventory_refresh import publish_inventory
    from based_inventory.pipeline.source_refresh import replay_source_plan

    now = datetime(2026, 11, 17, tzinfo=UTC)
    row = {
        "sku": "physical",
        "warehouse_id": "warehouse",
        "warehouse_identifier": "main",
        "on_hand": 7,
        "available": 0,
        "allocated": 7,
        "reserve_inventory": 0,
        "backorder": 0,
        "sell_ahead": 0,
        "updated_at": now.isoformat(),
        "product": {"name": "Example", "kit": False},
    }
    snapshot = build_snapshot(
        [{"product": "Example", "sku": "physical", "required": True}],
        [row],
        merchdrop_warehouse_id="warehouse",
        collected_at=now,
        now=now,
    )
    google = NativeSheets()
    for col in range(8, 16):
        google.data[(7, col)] = {
            "userEnteredValue": {"formulaValue": "=1"},
            "effectiveValue": {"numberValue": 1},
        }
    frozen = []

    def persist(verification):
        assert not google.writes
        receipt = {"status": "verified", "verification": verification}
        frozen.append(
            {
                "receipt": receipt,
                "digest": sha256(
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        )

    original_batch = google.batch

    def crash(*args):
        original_batch(*args)
        raise SystemExit("simulated process loss after provider accepted write")

    monkeypatch.setenv("PIPELINE_ALLOW_WRITES", "1")
    google.batch = crash
    with pytest.raises(SystemExit):
        publish_inventory(snapshot, google, mode="publish", before_write=persist)
    assert len(frozen) == 1
    google.batch = original_batch
    # The provider may now report another balance. Replay uses the durable
    # source intent, with no provider client or source collection call.
    row["available"] = 99
    assert replay_source_plan(google, frozen[0]) is True
    assert google.data[(7, 4)]["effectiveValue"] == {"numberValue": 0}


def test_older_pending_source_retry_cannot_write_after_later_day(monkeypatch):
    from unittest.mock import Mock

    from based_inventory.pipeline.source_refresh import refresh_sources
    from based_inventory.pipeline.store import Store
    from tests.test_pipeline_models import verified

    monkeypatch.setenv("PIPELINE_ALLOW_WRITES", "1")
    version = verified()
    store, google = Mock(spec=Store), Mock()
    store.load.return_value = version
    store.assert_destination_current.side_effect = ValueError(
        "A later reporting day owns this source destination"
    )
    result = refresh_sources(
        version.day,
        store,
        version.version_id,
        google,
        mode="publish",
        polar_token="unused",
        target_query="unused",
        target_workbook_id="unused",
    )
    assert result["status"] == "incomplete"
    assert all(r["status"] == "incomplete" for r in result["sources"].values())
    assert google.mock_calls == []
    store.plan_once.assert_not_called()
    store.record_receipt.assert_not_called()
