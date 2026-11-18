from dataclasses import replace
from datetime import date

import pytest

from based_inventory.pipeline.models import SalesRow
from based_inventory.pipeline.slack_report import SlackReport, SlackReportError
from based_inventory.sales_report_archive import SalesReportPackage
from based_inventory.velocity_archive import VelocityArchive
from tests.test_all_channel_sales import model
from tests.test_post_velocity_to_slack import SlackServer


def package(**changes):
    values = dict(
        day=date(2026, 11, 17),
        capture_id="a" * 64,
        history_ids={"2026-11-17": "a" * 64},
        summary="Every SKU, all four sales channels.",
        report=b"saved all-channel workbook",
        merchant=None,
    )
    return SalesReportPackage(**(values | changes))


def test_delivery_identity_binds_sources_history_exact_text_and_workbook():
    first = package()
    assert first.version_id == package().version_id
    assert (
        first.version_id
        != replace(first, capture_id="b" * 64, history_ids={"2026-11-17": "b" * 64}).version_id
    )
    assert first.version_id != replace(first, summary="Changed report definition").version_id
    assert first.version_id != replace(first, report=b"changed workbook").version_id
    assert (
        first.version_id
        != replace(first, history_ids={"2026-11-17": "a" * 64, "2026-11-16": "b" * 64}).version_id
    )


def test_saved_package_roundtrip_and_tampering_checks():
    first = package()
    assert SalesReportPackage.from_payload(first.payload()) == first
    payload = first.payload()
    payload["summary"] = "A misleading total"
    with pytest.raises(ValueError, match="hash"):
        SalesReportPackage.from_payload(payload)
    payload = first.payload()
    payload["report_base64"] = "Y2hhbmdlZA=="
    with pytest.raises(ValueError, match="hash"):
        SalesReportPackage.from_payload(payload)


def test_package_rejects_missing_or_wrong_source_identity():
    with pytest.raises(ValueError):
        package(capture_id="not-a-capture")
    with pytest.raises(ValueError):
        package(summary="")
    with pytest.raises(ValueError):
        package(report=b"")


def source(units=4):
    return model(rows=(SalesRow("BB2000S-1-6-1", "Sea Salt Spray", "Shopify", units),))


def save_package(storage, source_day):
    from based_inventory.sales_report_archive import SalesReportArchive

    archive = SalesReportArchive(storage)
    archive.save_day(source_day)
    report = package(
        day=source_day.day,
        capture_id=source_day.capture_id,
        history_ids={source_day.day.isoformat(): source_day.capture_id},
    )
    return archive, archive.save_package(report)


def test_all_channel_capture_and_exact_delivery_survive_reopening(tmp_path):
    server = SlackServer()
    client = SlackReport("token", "C_TEST", session=server)
    current = source()
    with VelocityArchive(tmp_path) as storage:
        archive, report = save_package(storage, current)
        client.publish(
            archive.delivery_store(report, "C_TEST"), report, summary_text=report.summary
        )
    with VelocityArchive(tmp_path) as storage:
        from based_inventory.sales_report_archive import SalesReportArchive

        archive = SalesReportArchive(storage)
        assert archive.latest_day(current.day) == current
        saved = archive.latest_package(current.day)
        assert saved == report
        client.publish(archive.delivery_store(saved, "C_TEST"), saved, summary_text=saved.summary)
    assert server.reservations == 1
    assert server.comments == [report.summary]


def test_pending_all_channel_upload_is_not_repeated(tmp_path):
    server = SlackServer("ambiguous")
    client = SlackReport("token", "C_TEST", session=server)
    with VelocityArchive(tmp_path) as storage:
        archive, report = save_package(storage, source())
        with pytest.raises(SlackReportError, match="ambiguous"):
            client.publish(
                archive.delivery_store(report, "C_TEST"), report, summary_text=report.summary
            )
    with VelocityArchive(tmp_path) as storage:
        from based_inventory.sales_report_archive import SalesReportArchive

        archive = SalesReportArchive(storage)
        saved = archive.latest_package(report.day)
        with pytest.raises(SlackReportError, match="ambiguous"):
            client.publish(
                archive.delivery_store(saved, "C_TEST"), saved, summary_text=saved.summary
            )
    assert server.reservations == 1


def test_changed_sources_or_report_require_reconciliation(tmp_path):
    with VelocityArchive(tmp_path) as storage:
        archive, report = save_package(storage, source())
        with pytest.raises(ValueError, match="different|reconcil"):
            archive.save_day(source(8))
        with pytest.raises(ValueError, match="different|reconcil"):
            archive.save_package(replace(report, summary="Changed definition"))
        assert archive.latest_package(report.day) == report


def test_corrupted_source_cannot_validate_a_saved_report(tmp_path):
    with VelocityArchive(tmp_path) as storage:
        archive, report = save_package(storage, source())
        record = storage._read("sales-day:" + report.day.isoformat())
        record["capture_id"] = "b" * 64
        storage._write("sales-day:" + report.day.isoformat(), record)
        with pytest.raises(ValueError, match="hash|capture"):
            archive.latest_package(report.day)
