"""Slack Block Kit client for #alerts-inventory posts."""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

_POST_URL = "https://slack.com/api/chat.postMessage"


class SlackClient:
    def __init__(self, token: str, channel: str, dry_run: bool = False) -> None:
        self.token = token
        self.channel = channel
        self.dry_run = dry_run

    def post_message(self, fallback_text: str, blocks: list[dict[str, Any]]) -> bool:
        payload = {
            "channel": self.channel,
            "text": fallback_text,
            "blocks": blocks,
            "unfurl_links": False,
        }

        if self.dry_run:
            print("[DRY_RUN] Slack post:")
            print(f"  channel: {self.channel}")
            print(f"  text: {fallback_text}")
            print(f"  blocks: {json.dumps(blocks, indent=2)}")
            return True

        try:
            response = requests.post(
                _POST_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            result = response.json()
        except requests.RequestException as exc:
            logger.error("Slack post failed: %s", exc)
            return False

        if not result.get("ok"):
            logger.error("Slack API error: %s", result.get("error", "unknown"))
            return False

        return True

    def upload_file(
        self,
        file_path: str,
        title: str | None = None,
        initial_comment: str | None = None,
        channel: str | None = None,
    ) -> bool:
        """Upload a file to a channel via Slack's external-upload flow.

        Three steps (the legacy files.upload is sunset): get an upload URL,
        POST the bytes to it, then complete the upload against the channel.
        Requires the bot to have the files:write scope and to be a member of
        the channel. Returns True on success.
        """
        import os

        channel = channel or self.channel
        filename = os.path.basename(file_path)
        size = os.path.getsize(file_path)

        if self.dry_run:
            print(f"[DRY_RUN] Slack upload {file_path} ({size} bytes) -> {channel}")
            if initial_comment:
                print(f"  comment: {initial_comment}")
            return True

        auth = {"Authorization": f"Bearer {self.token}"}
        try:
            r1 = requests.post(
                "https://slack.com/api/files.getUploadURLExternal",
                headers=auth,
                data={"filename": filename, "length": str(size)},
                timeout=30,
            )
            d1 = r1.json()
            if not d1.get("ok"):
                logger.error("Slack getUploadURLExternal error: %s", d1.get("error"))
                return False
            upload_url = d1["upload_url"]
            file_id = d1["file_id"]

            with open(file_path, "rb") as fh:
                r2 = requests.post(upload_url, files={"file": (filename, fh)}, timeout=180)
            if r2.status_code != 200:
                logger.error("Slack file POST failed: HTTP %s", r2.status_code)
                return False

            payload: dict[str, Any] = {
                "files": [{"id": file_id, "title": title or filename}],
                "channel_id": channel,
            }
            if initial_comment:
                payload["initial_comment"] = initial_comment
            r3 = requests.post(
                "https://slack.com/api/files.completeUploadExternal",
                headers={**auth, "Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            d3 = r3.json()
        except requests.RequestException as exc:
            logger.error("Slack upload failed: %s", exc)
            return False

        if not d3.get("ok"):
            logger.error("Slack completeUploadExternal error: %s", d3.get("error"))
            return False
        return True


def section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def divider() -> dict[str, Any]:
    return {"type": "divider"}


def header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}
