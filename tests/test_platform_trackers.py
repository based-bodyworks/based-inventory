import io
from datetime import date

import openpyxl
import pytest

from based_inventory.pipeline.models import digest
from based_inventory.pipeline.platform_trackers import (
    TABS,
    build_views,
    publish_views,
    verify_views,
)
from based_inventory.sales_report_archive import SalesReportPackage
from based_inventory.velocity_archive import VelocityArchive


def package(day=date(2026, 11, 18)):
    book = openpyxl.Workbook()
    book.remove(book.active)
    for title in ("SKU Velocity", "Sources", "Methods"):
        sheet = book.create_sheet(title)
        sheet.append(["Meaning", "Units"])
        sheet.append(["Amazon includes FBA", 42])
        sheet.append(["Missing history average", "n/a"])
    buffer = io.BytesIO()
    book.save(buffer)
    return SalesReportPackage(
        day, "a" * 64, {str(day): "a" * 64}, "Saved report", buffer.getvalue()
    )


class Google:
    def __init__(self):
        self.tabs = {}
        self.writes = []
        self.fail_after = None

    def platform_read(self, file, tab_id, title):
        current = self.tabs.get((file, tab_id))
        if current and current[0] != title:
            raise ValueError("Unexpected tab identity")
        return current[1] if current else None

    def platform_write(self, file, tab_id, title, rows):
        self.tabs[file, tab_id] = (title, rows)
        self.writes.append((file, tab_id))
        if self.fail_after == len(self.writes):
            raise RuntimeError("Lost response after provider accepted write")


def test_views_copy_exact_workbook_cells_and_bind_package_identity():
    report = package()
    views = build_views(report)
    assert set(views) == {"SKU Velocity", "Sources", "Methods"}
    for rows in views.values():
        assert report.version_id in rows[1]
        assert digest(report.report) in rows[2]
        assert rows[5:] == [
            ["Meaning", "Units"],
            ["Amazon includes FBA", 42],
            ["Missing history average", "n/a"],
        ]


def test_retries_do_not_rewrite_verified_views_and_monitor_reads_live(tmp_path):
    google = Google()
    with VelocityArchive(tmp_path) as storage:
        report = package()
        publish_views(storage, report, google)
        assert len(google.writes) == 6
        publish_views(storage, report, google)
        assert len(google.writes) == 6
        verify_views(storage, report, google)
        key = next(iter(google.tabs))
        google.tabs[key][1][6][1] = 999
        with pytest.raises(ValueError, match="differs"):
            verify_views(storage, report, google)
        with pytest.raises(ValueError, match="outside"):
            publish_views(storage, report, google)


def test_lost_response_reconciles_same_values_and_blocks_older_day(tmp_path):
    google = Google()
    google.fail_after = 2
    with VelocityArchive(tmp_path) as storage:
        report = package()
        with pytest.raises(RuntimeError):
            publish_views(storage, report, google)
        google.fail_after = None
        publish_views(storage, report, google)
        verify_views(storage, report, google)
        assert len(google.writes) == 6
        with pytest.raises(ValueError, match="later"):
            publish_views(storage, package(date(2026, 11, 17)), google)
        assert len(google.writes) == 6


def test_formula_workbook_and_existing_foreign_tab_rejected_before_writes(tmp_path):
    report = package()
    book = openpyxl.load_workbook(io.BytesIO(report.report))
    book["Methods"]["A2"] = "=1+1"
    output = io.BytesIO()
    book.save(output)
    from dataclasses import replace

    with pytest.raises(ValueError, match="formula"):
        build_views(replace(report, report=output.getvalue()))
    google = Google()
    file, tab_id, title, _ = TABS[-1]
    google.tabs[file, tab_id] = ("Owner sheet", [["preserve"]])
    with VelocityArchive(tmp_path) as storage, pytest.raises(ValueError):
        publish_views(storage, report, google)
    assert not google.writes


def test_join_uses_saved_report_checks_slack_and_readonly_mode(tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import timedelta

    from based_inventory.pipeline.platform_trackers import check_or_publish
    from based_inventory.sales_report_archive import SalesReportArchive
    from tests.test_all_channel_sales import model

    report = package()
    with VelocityArchive(tmp_path) as storage:
        archive = SalesReportArchive(storage)
        records = []
        from based_inventory.all_channel_sales import SalesDay, decode_source_proof, encode_proof

        for i in range(7):
            payload = model(day=report.day - timedelta(days=i)).payload()
            proof = decode_source_proof(payload["sources"]["Target"])
            proof["parsed"]["safe_to_write"] = True
            proof["parsed"]["unmapped_products"] = []
            encoded, identity = encode_proof(proof)
            payload["sources"]["Target"]["raw_proof_gzip"] = encoded
            payload["sources"]["Target"]["raw_sha256"] = identity
            records.append(archive.save_day(SalesDay.from_payload(payload)))
        report = archive.save_package(
            replace(
                report,
                capture_id=records[0].capture_id,
                history_ids={str(record.day): record.capture_id for record in records},
            )
        )
        delivery = archive.delivery_store(report, "C_TEST")
        delivery.record_receipt(
            report.version_id, "slack", digest(report.report), "F_TEST", status="pending"
        )

    class Slack:
        channel = "C_TEST"

        def verify(self, file_id, saved):
            assert file_id == "F_TEST" and saved.version_id == report.version_id

    google = Google()
    monkeypatch.setenv("PIPELINE_ALLOW_WRITES", "1")
    with pytest.raises(ValueError, match="delivery"):
        check_or_publish(tmp_path, report.day, google, Slack(), publish=True)
    assert not google.writes
    with VelocityArchive(tmp_path) as storage:
        SalesReportArchive(storage).delivery_store(report, "C_TEST").record_receipt(
            report.version_id, "slack", digest(report.report), "F_TEST", status="verified"
        )
    assert (
        check_or_publish(tmp_path, report.day, google, Slack(), publish=True) == report.version_id
    )
    count = len(google.writes)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    monkeypatch.setenv("PIPELINE_ALLOW_WRITES", "0")
    check_or_publish(tmp_path, report.day, google, Slack())
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert len(google.writes) == count
    with pytest.raises(PermissionError):
        check_or_publish(tmp_path, report.day, google, Slack(), publish=True)


def test_readonly_archive_does_not_migrate_or_allow_writes(tmp_path, monkeypatch):
    from unittest.mock import Mock

    connection = Mock()
    connect = Mock(return_value=connection)
    monkeypatch.setattr("based_inventory.velocity_archive.psycopg.connect", connect)
    with VelocityArchive("postgresql://synthetic/test", read_only=True) as storage:
        connection.execute.assert_not_called()
        assert connect.call_args.kwargs["options"] == "-c default_transaction_read_only=on"
        with pytest.raises(PermissionError):
            storage._write("test", {})
