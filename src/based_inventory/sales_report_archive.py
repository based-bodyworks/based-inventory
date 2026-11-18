"""Freeze the source captures, text and workbook behind each daily Slack send."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType

from based_inventory.pipeline.models import VerifiedDay, canonical, digest
from based_inventory.velocity_archive import VelocityArchive


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class SalesReportPackage:
    day: date
    capture_id: str
    history_ids: Mapping[str, str]
    summary: str
    report: bytes
    merchant: VerifiedDay | None = None

    def __post_init__(self):
        if (
            type(self.day) is not date
            or not _hash(self.capture_id)
            or not isinstance(self.summary, str)
            or not self.summary.strip()
            or type(self.report) is not bytes
            or not self.report
        ):
            raise ValueError("Invalid complete sales report package")
        history = dict(self.history_ids)
        if history.get(self.day.isoformat()) != self.capture_id:
            raise ValueError("Sales report is missing its current source capture")
        for key, value in history.items():
            day = date.fromisoformat(key)
            if not self.day - timedelta(days=6) <= day <= self.day or not _hash(value):
                raise ValueError("Invalid sales report history identity")
        if self.merchant is not None and (
            not isinstance(self.merchant, VerifiedDay) or self.merchant.day != self.day
        ):
            raise ValueError("Warehouse context has a different reporting date")
        object.__setattr__(self, "history_ids", MappingProxyType(history))

    def payload(self):
        merchant = None
        if self.merchant is not None:
            merchant = {
                "payload": self.merchant.payload(),
                "report_base64": base64.b64encode(self.merchant.report).decode(),
            }
        return {
            "schema": 1,
            "day": self.day.isoformat(),
            "capture_id": self.capture_id,
            "history_ids": dict(self.history_ids),
            "summary": self.summary,
            "summary_sha256": digest(self.summary.encode()),
            "report_base64": base64.b64encode(self.report).decode(),
            "report_sha256": digest(self.report),
            "merchant": merchant,
        }

    @property
    def version_id(self):
        return digest(canonical(self.payload()))

    @classmethod
    def from_payload(cls, payload):
        try:
            if payload["schema"] != 1:
                raise ValueError("Unsupported sales package schema")
            report = base64.b64decode(payload["report_base64"], validate=True)
            if (
                digest(report) != payload["report_sha256"]
                or digest(payload["summary"].encode()) != payload["summary_sha256"]
            ):
                raise ValueError("Saved sales report text or workbook hash mismatch")
            merchant = None
            if payload["merchant"] is not None:
                warehouse = payload["merchant"]
                merchant = VerifiedDay.from_payload(
                    warehouse["payload"],
                    base64.b64decode(warehouse["report_base64"], validate=True),
                )
            result = cls(
                day=date.fromisoformat(payload["day"]),
                capture_id=payload["capture_id"],
                history_ids=payload["history_ids"],
                summary=payload["summary"],
                report=report,
                merchant=merchant,
            )
            if result.payload() != payload:
                raise ValueError("Non-canonical saved sales package")
            return result
        except (KeyError, TypeError, AttributeError, binascii.Error):
            raise ValueError("Invalid saved sales report package") from None


class SalesReportArchive:
    """Keep complete native sales captures separate from warehouse history."""

    def __init__(self, storage: VelocityArchive):
        self.storage = storage

    def latest_day(self, day: date):
        from based_inventory.all_channel_sales import SalesDay

        record = self.storage._read(f"sales-day:{day}")
        if record is None:
            return None
        try:
            current = SalesDay.from_payload(record["payload"])
            if current.day != day or current.capture_id != record["capture_id"]:
                raise ValueError("Saved sales source capture hash mismatch")
            return current
        except (KeyError, TypeError):
            raise ValueError("Invalid saved sales source capture") from None

    def save_day(self, current):
        from based_inventory.all_channel_sales import SalesDay

        if not isinstance(current, SalesDay):
            raise ValueError("A complete checked all-channel sales capture is required")
        self.storage._write(
            f"sales-day:{current.day}",
            {"capture_id": current.capture_id, "payload": current.payload()},
            once=True,
        )
        saved = self.latest_day(current.day)
        if saved is None or saved.capture_id != current.capture_id:
            raise ValueError("This date has different sales sources; reconcile before publication")
        return saved

    def window(self, end: date):
        result = {}
        for offset in range(7):
            day = end - timedelta(days=offset)
            current = self.latest_day(day)
            if current is not None:
                result[day] = current
        return result

    def _verify_sources(self, package: SalesReportPackage):
        for key, identity in package.history_ids.items():
            current = self.latest_day(date.fromisoformat(key))
            if current is None or current.capture_id != identity:
                raise ValueError("Saved sales report source capture is missing or changed")

    def latest_package(self, day: date):
        record = self.storage._read(f"sales-package:{day}")
        if record is None:
            return None
        try:
            package = SalesReportPackage.from_payload(record["payload"])
            if package.day != day or package.version_id != record["version_id"]:
                raise ValueError("Saved sales report package hash mismatch")
        except (KeyError, TypeError):
            raise ValueError("Invalid saved sales report package") from None
        self._verify_sources(package)
        return package

    def save_package(self, package: SalesReportPackage):
        self._verify_sources(package)
        self.storage._write(
            f"sales-package:{package.day}",
            {"version_id": package.version_id, "payload": package.payload()},
            once=True,
        )
        saved = self.latest_package(package.day)
        if saved is None or saved.version_id != package.version_id:
            raise ValueError("This date has a different frozen report; reconcile before sending")
        return saved

    def delivery_store(self, package: SalesReportPackage, channel: str):
        return self.storage.delivery_store(
            package,
            channel,
            lookup=self.latest_package,
            key_prefix="all-channel-delivery",
        )
