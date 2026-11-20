"""Reconcile a known Slack file before retrying an ambiguous publication."""

import time
from datetime import timedelta
from urllib.parse import urlparse

import requests

from .models import digest


class SlackReportError(RuntimeError):
    """A redacted Slack publication/readback failure."""


def summary(version, history=None):
    from based_inventory.daily_velocity_report import render_report

    return render_report(version, history or {})


class SlackReport:
    def __init__(self, token, channel, *, session=None):
        if not token or not channel:
            raise ValueError("Slack bot and report channel configuration required")
        self.token, self.channel = token, channel
        self.session = session or requests.Session()

    def call(self, method, data):
        if method not in {
            "files.info",
            "files.getUploadURLExternal",
            "files.completeUploadExternal",
        }:
            raise ValueError("Unexpected Slack publication operation")
        kwargs = {"json" if method == "files.completeUploadExternal" else "data": data}
        try:
            response = self.session.post(
                "https://slack.com/api/" + method,
                headers={"Authorization": "Bearer " + self.token},
                timeout=30,
                **kwargs,
            )
            if response.status_code != 200:
                raise SlackReportError(f"Slack API HTTP {response.status_code}")
            payload = response.json()
            if payload.get("ok") is not True:
                raise SlackReportError("Slack API operation rejected")
            return payload
        except (requests.RequestException, ValueError):
            raise SlackReportError("Slack API response unavailable") from None

    def info(self, file_id):
        return self.call("files.info", {"file": file_id})["file"]

    def download(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "files.slack.com":
            raise ValueError("Unexpected Slack file origin")
        try:
            response = self.session.get(
                url,
                headers={"Authorization": "Bearer " + self.token},
                timeout=60,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise SlackReportError("Slack file download unavailable")
            return response.content
        except requests.RequestException:
            raise SlackReportError("Slack file download response unavailable") from None

    def verify(self, file_id, version):
        info = self.info(file_id)
        channels = set(info.get("channels", [])) | set(info.get("groups", []))
        for scope in ("public", "private"):
            channels.update(info.get("shares", {}).get(scope, {}))
        if self.channel not in channels or info.get("size") != len(version.report):
            raise SlackReportError("Saved Slack report is missing from its required channel")
        if digest(self.download(info["url_private_download"])) != digest(version.report):
            raise SlackReportError("Saved Slack report hash mismatch")

    def _verify_delivery(self, file_id, version):
        # Slack can acknowledge completion before its file readback is ready.
        # Retry only the existing file; never allocate or complete another upload.
        for attempt in range(5):
            try:
                self.verify(file_id, version)
                return
            except (SlackReportError, KeyError, ValueError):
                if attempt == 4:
                    raise
                time.sleep(2**attempt)

    def publish(
        self, store, version, *, summary_text: str | None = None, report_title: str | None = None
    ):
        store.assert_current(version.version_id)
        receipt = store.receipts(version.version_id).get("slack")
        if receipt:
            file_id = receipt["provider_id"]
            try:
                self._verify_delivery(file_id, version)
            except (SlackReportError, KeyError, ValueError):
                raise SlackReportError(
                    "Slack publication remains ambiguous; reconcile the reserved file before any reupload"
                ) from None
            store.record_receipt(version.version_id, "slack", digest(version.report), file_id)
            return file_id
        if summary_text is None:
            history = {}
            for offset in range(7):
                day = version.day - timedelta(days=offset)
                record = store.latest(day)
                if record is not None:
                    history[day] = record
            summary_text = summary(version, history)
        reservation = self.call(
            "files.getUploadURLExternal",
            {"filename": f"daily-velocity_{version.day}.xlsx", "length": str(len(version.report))},
        )
        file_id, upload_url = reservation["file_id"], reservation["upload_url"]
        parsed = urlparse(upload_url)
        if parsed.scheme != "https" or parsed.hostname != "files.slack.com":
            raise ValueError("Unexpected Slack file origin")
        store.record_receipt(
            version.version_id, "slack", digest(version.report), file_id, status="pending"
        )
        try:
            upload = self.session.post(
                upload_url,
                data=version.report,
                headers={"Content-Type": "application/octet-stream"},
                timeout=180,
                allow_redirects=False,
            )
            if upload.status_code != 200:
                raise SlackReportError("Slack file upload rejected")
            self.call(
                "files.completeUploadExternal",
                {
                    "files": [
                        {
                            "id": file_id,
                            "title": report_title or f"BASED Merchant Sales {version.day}",
                        }
                    ],
                    "channel_id": self.channel,
                    "initial_comment": summary_text,
                },
            )
        except (requests.RequestException, SlackReportError):
            # Do not allocate another file. A successful-but-lost completion is reconciled below.
            pass
        try:
            self._verify_delivery(file_id, version)
        except (SlackReportError, KeyError, ValueError):
            raise SlackReportError(
                "Slack publication remains ambiguous; reserved file requires reconciliation"
            ) from None
        store.record_receipt(version.version_id, "slack", digest(version.report), file_id)
        return file_id
