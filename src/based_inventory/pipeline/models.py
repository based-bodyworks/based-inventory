"""Immutable, non-customer sales records shared by every publisher."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType

CHANNELS = ("Shopify", "TIKTOK", "Amazon FBM")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class SalesRow:
    sku: str
    product: str
    channel: str
    units: int


@dataclass(frozen=True)
class Coverage:
    before: int
    after: int
    fetched_unique: int
    complete: bool

    def validate(self) -> None:
        counts = (self.before, self.after, self.fetched_unique)
        if (
            any(type(n) is not int or n < 0 for n in counts)
            or len(set(counts)) != 1
            or self.complete is not True
        ):
            raise ValueError("Unverified order/line coverage")


@dataclass(frozen=True)
class VerifiedDay:
    day: date
    rows: tuple[SalesRow, ...]
    coverage: Coverage
    excluded_orders: Mapping[str, int]
    catalog_hash: str
    extractor_commit: str
    report: bytes
    captured_at: datetime

    def __post_init__(self) -> None:
        self.coverage.validate()
        if (
            type(self.day) is not date
            or self.captured_at.tzinfo is None
            or self.day >= self.captured_at.astimezone(UTC).date()
        ):
            raise ValueError("Sales version must cover a complete UTC day")
        if not self.report or type(self.report) is not bytes:
            raise ValueError("Saved report is required")
        if not re.fullmatch(r"[a-f0-9]{64}", self.catalog_hash):
            raise ValueError("Invalid catalog hash")
        if not re.fullmatch(r"[a-f0-9]{40}", self.extractor_commit):
            raise ValueError("Invalid extractor commit")
        rows = tuple(sorted(self.rows, key=lambda r: (r.sku, r.channel)))
        seen = set()
        for row in rows:
            if not row.sku or not row.product or row.channel not in CHANNELS:
                raise ValueError("Invalid sales identity")
            if type(row.units) is not int or row.units < 0:
                raise ValueError("Invalid sales units")
            key = (row.sku, row.channel)
            if key in seen:
                raise ValueError("duplicate sales key")
            seen.add(key)
        excluded = dict(self.excluded_orders)
        if any(
            not isinstance(k, str) or not k or type(v) is not int or v < 0
            for k, v in excluded.items()
        ):
            raise ValueError("Invalid excluded order counts")
        if sum(excluded.values()) > self.coverage.fetched_unique:
            raise ValueError("Excluded orders exceed source coverage")
        if self.coverage.fetched_unique == 0 and any(r.units for r in rows):
            raise ValueError("Sales units exceed zero-order coverage")
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "excluded_orders", MappingProxyType(excluded))

    @property
    def totals(self) -> dict[str, int]:
        return {c: sum(r.units for r in self.rows if r.channel == c) for c in CHANNELS}

    def payload(self) -> dict:
        return {
            "schema": 1,
            "extract_schema": 7,
            "day": self.day.isoformat(),
            "rows": [asdict(r) for r in self.rows],
            "coverage": asdict(self.coverage),
            "excluded_orders": dict(self.excluded_orders),
            "catalog_hash": self.catalog_hash,
            "extractor_commit": self.extractor_commit,
            "report_sha256": digest(self.report),
            "captured_at": self.captured_at.astimezone(UTC).isoformat(),
        }

    @property
    def version_id(self) -> str:
        return digest(canonical(self.payload()))

    @classmethod
    def from_payload(cls, payload: dict, report: bytes) -> VerifiedDay:
        if payload.get("schema") != 1 or payload.get("extract_schema") != 7:
            raise ValueError("Unsupported verified sales schema")
        if digest(report) != payload["report_sha256"]:
            raise ValueError("Saved report hash mismatch")
        result = cls(
            day=date.fromisoformat(payload["day"]),
            rows=tuple(SalesRow(**r) for r in payload["rows"]),
            coverage=Coverage(**payload["coverage"]),
            excluded_orders=payload["excluded_orders"],
            catalog_hash=payload["catalog_hash"],
            extractor_commit=payload["extractor_commit"],
            report=report,
            captured_at=datetime.fromisoformat(payload["captured_at"]),
        )
        if result.payload() != payload:
            raise ValueError("Non-canonical saved sales payload")
        return result
