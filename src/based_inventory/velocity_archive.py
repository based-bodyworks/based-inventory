"""Receipt-backed daily velocity history, separate from quantity-alert state.

The active daily job has no persistent filesystem on Render. Redis keeps each
verified day for 45 days; the much smaller publication receipts do not expire,
so an old manual rerun cannot blindly send a duplicate after history expires.
Local directories support tests, reviewed imports and network-free previews.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import json
import os
from contextlib import contextmanager, suppress
from datetime import date, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

from based_inventory.pipeline.models import VerifiedDay, digest

PREFIX = "based-inventory:daily-velocity:v1:"
HISTORY_SECONDS = 45 * 86400


class VelocityArchive:
    def __init__(self, location: Path | str):
        self.location = str(location)
        self._redis = None
        self._directory = None
        if self.location.startswith(("redis://", "rediss://")):
            import redis

            self._redis = redis.from_url(
                self.location, decode_responses=True, socket_timeout=15, socket_connect_timeout=15
            )
        else:
            self._directory = Path(location)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._redis is not None:
            self._redis.close()

    def _path(self, key: str) -> Path:
        return self._directory / (quote(key, safe="") + ".json")

    def _read(self, key: str) -> dict | None:
        if self._redis is not None:
            raw = self._redis.get(PREFIX + key)
        else:
            path = self._path(key)
            raw = path.read_text() if path.exists() else None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError("Invalid daily velocity archive JSON") from None
        if not isinstance(value, dict):
            raise ValueError("Invalid daily velocity archive record")
        return value

    def _write(self, key: str, value: dict, *, once: bool = False) -> None:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if self._redis is not None:
            if once:
                self._redis.set(PREFIX + key, text, nx=True, ex=HISTORY_SECONDS)
            else:
                self._redis.set(PREFIX + key, text)
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", dir=self._directory, delete=False) as file:
            temporary = Path(file.name)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        try:
            if once:
                with suppress(FileExistsError):
                    os.link(temporary, self._path(key))
            else:
                os.replace(temporary, self._path(key))
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def lock(self, day: date):
        """Serialize a day's archive and send without locking stock-alert jobs."""
        if self._redis is not None:
            lock = self._redis.lock(PREFIX + f"lock:{day}", timeout=900, blocking=False)
            if not lock.acquire(blocking=False):
                raise RuntimeError("Daily velocity run is already running for this date")
            try:
                yield
            finally:
                lock.release()
            return
        self._directory.mkdir(parents=True, exist_ok=True)
        with (self._directory / f".run-{day}.lock").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Daily velocity run is already running for this date") from None
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def latest(self, day: date) -> VerifiedDay | None:
        record = self._read(f"day:{day}")
        if record is None:
            return None
        try:
            report = base64.b64decode(record["report_base64"], validate=True)
            version = VerifiedDay.from_payload(record["payload"], report)
            if version.day != day or version.version_id != record["version_id"]:
                raise ValueError("Daily velocity archive identity/hash mismatch")
        except (KeyError, TypeError, binascii.Error):
            raise ValueError("Invalid daily velocity archive proof") from None
        return version

    def save(self, version: VerifiedDay) -> VerifiedDay:
        """Reuse an identical rerun; refuse a correction instead of hiding it.

        A regenerated workbook can have different ZIP timestamps. Reusing the
        first report when the actual source evidence and rows match preserves
        the original Slack receipt and makes an ordinary retry idempotent.
        """
        record = {
            "version_id": version.version_id,
            "payload": version.payload(),
            "report_base64": base64.b64encode(version.report).decode(),
        }
        self._write(f"day:{version.day}", record, once=True)
        saved = self.latest(version.day)
        if saved is None:
            raise ValueError("Daily velocity archive write was not saved")
        if (
            saved.rows != version.rows
            or saved.coverage != version.coverage
            or saved.excluded_orders != version.excluded_orders
            or saved.catalog_hash != version.catalog_hash
        ):
            raise ValueError(
                "This date already has different verified sales; reconcile the correction before sending"
            )
        return saved

    def window(self, end: date) -> dict[date, VerifiedDay]:
        result = {}
        for offset in range(7):
            day = end - timedelta(days=offset)
            version = self.latest(day)
            if version is not None:
                result[day] = version
        return result

    def delivery_store(self, version: VerifiedDay, channel: str):
        return _DeliveryStore(self, version, channel)


class _DeliveryStore:
    """Small Store interface used by the existing reconciled Slack uploader."""

    def __init__(self, archive: VelocityArchive, version: VerifiedDay, channel: str):
        self.archive, self.version, self.channel = archive, version, channel
        self.key = f"delivery:{version.day}:{channel}"

    def assert_current(self, version_id: str) -> None:
        saved = self.archive.latest(self.version.day)
        if version_id != self.version.version_id or saved is None or saved.version_id != version_id:
            raise ValueError("The current verified daily sales record changed or is unavailable")

    def receipts(self, version_id: str) -> dict:
        self.assert_current(version_id)
        saved = self.archive._read(self.key)
        if saved is None:
            return {}
        if saved.get("version_id") != version_id or saved.get("channel") != self.channel:
            raise ValueError("A different version already owns this day's Slack publication")
        receipt = saved.get("receipt")
        if (
            not isinstance(receipt, dict)
            or receipt.get("status") not in {"pending", "verified", "ambiguous"}
            or receipt.get("digest") != digest(self.version.report)
            or not isinstance(receipt.get("provider_id"), str)
            or not receipt["provider_id"]
        ):
            raise ValueError("Invalid saved daily velocity delivery receipt")
        return {"slack": receipt}

    def record_receipt(
        self,
        version_id: str,
        destination: str,
        report_digest: str,
        provider_id: str | None = None,
        status: str = "verified",
    ) -> None:
        self.assert_current(version_id)
        if (
            destination != "slack"
            or report_digest != digest(self.version.report)
            or not provider_id
            or status not in {"pending", "verified", "ambiguous"}
        ):
            raise ValueError("Invalid daily velocity report digest or receipt")
        existing = self.receipts(version_id).get("slack")
        if existing and existing["provider_id"] != provider_id:
            raise ValueError("Another Slack file is already reserved for this date")
        self.archive._write(
            self.key,
            {
                "version_id": version_id,
                "channel": self.channel,
                "receipt": {"digest": report_digest, "provider_id": provider_id, "status": status},
            },
        )
