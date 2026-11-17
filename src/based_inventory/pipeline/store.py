"""Durable snapshots, publication plans and receipts on a direct Postgres connection."""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import VerifiedDay

LOCK_ID = 725013417
DESTINATIONS = frozenset(
    {
        "history",
        "canonical",
        "rolling",
        "erp",
        "inventory",
        "slack",
        "target",
        "amazon",
        "workbook",
        "monitor",
    }
)


class BusyPipelineError(RuntimeError):
    """Another pipeline process holds the shared source/publisher lock."""


class Store:
    def __init__(self, dsn: str):
        # A direct/session-pooling connection is required: advisory locks are session scoped.
        self.connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()

    def migrate(self) -> None:
        sql = Path(__file__).resolve().parents[3] / "migrations/001_sales_pipeline.sql"
        with self.connection.transaction():
            self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID + 1,))
            self.connection.execute(sql.read_text())

    @contextmanager
    def lock_merchant_pull(self):
        if not self.connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (LOCK_ID,)
        ).fetchone()["locked"]:
            raise BusyPipelineError("Another cloud sales or tracker run is active")
        try:
            yield
        finally:
            self.connection.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))

    def save_version(self, version: VerifiedDay) -> str:
        self.connection.execute(
            "INSERT INTO sales_versions(version_id, day, captured_at, payload, report) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                version.version_id,
                version.day,
                version.captured_at,
                Jsonb(version.payload()),
                version.report,
            ),
        )
        if self.load(version.version_id) != version:
            raise ValueError("Existing sales version differs")
        return version.version_id

    def load(self, version_id: str) -> VerifiedDay:
        row = self.connection.execute(
            "SELECT payload, report FROM sales_versions WHERE version_id=%s", (version_id,)
        ).fetchone()
        if not row:
            raise ValueError("Unknown sales version")
        result = VerifiedDay.from_payload(row["payload"], bytes(row["report"]))
        if result.version_id != version_id:
            raise ValueError("Saved sales version hash mismatch")
        return result

    def latest(self, day: date) -> VerifiedDay | None:
        row = self.connection.execute(
            "SELECT version_id FROM sales_versions WHERE day=%s ORDER BY captured_at DESC, created_at DESC LIMIT 1",
            (day,),
        ).fetchone()
        return self.load(row["version_id"]) if row else None

    def assert_current(self, version_id: str) -> None:
        version = self.load(version_id)
        if self.latest(version.day).version_id != version_id:
            raise ValueError("A newer verified version exists; refusing stale publication")

    def plan_once(self, version_id: str, destination: str, plan: dict) -> dict:
        self._destination(destination)
        self.assert_current(version_id)
        self.connection.execute(
            "INSERT INTO pipeline_plans VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (version_id, destination, Jsonb(plan)),
        )
        return self.connection.execute(
            "SELECT plan FROM pipeline_plans WHERE version_id=%s AND destination=%s",
            (version_id, destination),
        ).fetchone()["plan"]

    @staticmethod
    def _destination(destination: str) -> None:
        if destination not in DESTINATIONS:
            raise ValueError("Unknown publication destination")

    def record_receipt(
        self,
        version_id: str,
        destination: str,
        digest: str,
        provider_id: str | None = None,
        status: str = "verified",
    ) -> None:
        self._destination(destination)
        self.assert_current(version_id)
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or status not in {
            "pending",
            "verified",
            "ambiguous",
        }:
            raise ValueError("Invalid publication receipt")
        self.connection.execute(
            """INSERT INTO pipeline_receipts(version_id,destination,status,digest,provider_id) VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT(version_id,destination) DO UPDATE SET status=excluded.status,digest=excluded.digest,
            provider_id=excluded.provider_id,updated_at=now()""",
            (version_id, destination, status, digest, provider_id),
        )

    def receipts(self, version_id: str) -> dict:
        rows = self.connection.execute(
            "SELECT * FROM pipeline_receipts WHERE version_id=%s", (version_id,)
        ).fetchall()
        return {row["destination"]: row for row in rows}

    def begin_run(self, kind: str, day: date) -> str:
        if kind not in {"sales", "trackers", "monitor"}:
            raise ValueError("Unknown run type")
        run_id = str(uuid4())
        self.connection.execute(
            "INSERT INTO pipeline_runs(run_id,kind,day,status) VALUES (%s,%s,%s,'running')",
            (run_id, kind, day),
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        version_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if status not in {"verified", "failed"} or (
            error_code and not re.fullmatch(r"[a-z_]{1,64}", error_code)
        ):
            raise ValueError("Invalid run outcome")
        self.connection.execute(
            "UPDATE pipeline_runs SET status=%s,version_id=%s,error_code=%s,updated_at=now() WHERE run_id=%s",
            (status, version_id, error_code, run_id),
        )

    def latest_run(self, kind: str, day: date) -> dict | None:
        return self.connection.execute(
            "SELECT * FROM pipeline_runs WHERE kind=%s AND day=%s ORDER BY started_at DESC LIMIT 1",
            (kind, day),
        ).fetchone()
