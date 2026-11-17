from datetime import UTC, date, datetime

import pytest

from based_inventory.pipeline.models import Coverage, SalesRow, VerifiedDay


def verified(**overrides):
    args = dict(
        day=date(2026, 11, 16),
        rows=(SalesRow("sku", "Product", "Shopify", 4),),
        coverage=Coverage(2, 2, 2, True),
        excluded_orders={"Manual Order": 1},
        catalog_hash="a" * 64,
        extractor_commit="b" * 40,
        report=b"saved workbook",
        captured_at=datetime(2026, 11, 17, 12, tzinfo=UTC),
    )
    return VerifiedDay(**(args | overrides))


def test_version_covers_report_and_source_evidence():
    first = verified()
    assert first.version_id == verified().version_id
    assert first.version_id != verified(report=b"changed workbook").version_id
    assert first.version_id != verified(coverage=Coverage(3, 3, 3, True)).version_id
    assert first.totals == {"Shopify": 4, "TIKTOK": 0, "Amazon FBM": 0}


@pytest.mark.parametrize(
    "coverage", [Coverage(2, 3, 2, True), Coverage(2, 2, 1, True), Coverage(2, 2, 2, False)]
)
def test_incomplete_source_cannot_become_a_version(coverage):
    with pytest.raises(ValueError, match="coverage"):
        verified(coverage=coverage)


def test_incomplete_day_and_duplicate_or_invalid_rows_are_rejected():
    with pytest.raises(ValueError, match="complete UTC day"):
        verified(day=date(2026, 11, 17))
    with pytest.raises(ValueError, match="duplicate"):
        verified(rows=(SalesRow("sku", "Product", "Shopify", 4),) * 2)
    for value in (-1, 0.5, True):
        with pytest.raises(ValueError, match="units"):
            verified(rows=(SalesRow("sku", "Product", "Shopify", value),))


def test_serialization_is_verified_against_hashes():
    version = verified()
    assert VerifiedDay.from_payload(version.payload(), version.report) == version
    with pytest.raises(ValueError, match="report hash"):
        VerifiedDay.from_payload(version.payload(), b"wrong")
