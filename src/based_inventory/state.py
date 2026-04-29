"""Persistent alert state for dedup across runs.

Stores the last-observed severity tier per product (quantity alerts)
and the set of currently-flagged ATC anomalies (ATC audit).

Backend dispatch:
- If the location starts with `redis://` or `rediss://`, a Redis
  backend is used (single JSON blob at key REDIS_STATE_KEY).
- Otherwise the location is a filesystem path (JSON file).

Payload shape (both backends):
{
  "quantity_tiers": {"Shampoo": 500, "Conditioner": 1000},
  "atc_flags": {
    "<variant_gid>::<url>::<flag_type>": {"first_seen_at": "...", "last_seen_at": "..."}
  }
}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REDIS_STATE_KEY = "based-inventory:alert-state"


def _is_redis_url(location: str) -> bool:
    return location.startswith("redis://") or location.startswith("rediss://")


def _read_redis(url: str) -> dict[str, Any]:
    import redis

    client = redis.from_url(url, decode_responses=True, socket_timeout=10)
    try:
        raw = client.get(REDIS_STATE_KEY)
    finally:
        client.close()
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Redis state at %s is not valid JSON: %s; starting fresh", url, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_redis(url: str, payload: dict[str, Any]) -> None:
    import redis

    client = redis.from_url(url, decode_responses=True, socket_timeout=10)
    try:
        client.set(REDIS_STATE_KEY, json.dumps(payload))
    finally:
        client.close()


def _read_file(p: Path) -> dict[str, Any]:
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load state from %s: %s; starting fresh", p, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_file(p: Path, payload: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2))


def _coerce(
    data: dict[str, Any],
) -> tuple[dict[str, int], dict[str, dict[str, str]], str | None]:
    qt = data.get("quantity_tiers", {})
    af = data.get("atc_flags", {})
    sv = data.get("schema_version")
    if not isinstance(qt, dict):
        logger.warning("state 'quantity_tiers' is not an object; ignoring")
        qt = {}
    if not isinstance(af, dict):
        logger.warning("state 'atc_flags' is not an object; ignoring")
        af = {}
    if sv is not None and not isinstance(sv, str):
        sv = None
    return qt, af, sv


# Bump this when a code change makes the OLD state no longer correct.
# Examples: SKU/title key migration, ATC regex change that retires
# previously-recorded false-positive flags, dedup logic refactor.
# On load, if state.schema_version != CURRENT_SCHEMA_VERSION, all
# dedup state is wiped (loud log) so prior false positives don't
# silently keep matching new code.
#
# History:
#   None  -> v1 (legacy: title-keyed quantity_tiers, broken ATC regex)
#   v1    -> v2 (2026-04-28: SKU-keyed quantity_tiers via ShipHero
#                rewire; tightened ATC regex with $-price suffix support;
#                clears stale title-keyed entries and 5 false-positive
#                ATC flags from the prior DRY_RUN cycles).
CURRENT_SCHEMA_VERSION = "v2"


@dataclass
class AlertState:
    quantity_tiers: dict[str, int] = field(default_factory=dict)
    atc_flags: dict[str, dict[str, str]] = field(default_factory=dict)
    schema_version: str = CURRENT_SCHEMA_VERSION

    @classmethod
    def load(cls, location: Path | str) -> AlertState:
        loc = str(location)
        data = _read_redis(loc) if _is_redis_url(loc) else _read_file(Path(loc))
        qt, af, sv = _coerce(data)
        if sv != CURRENT_SCHEMA_VERSION:
            logger.warning(
                "AlertState schema version %r != current %r; clearing "
                "quantity_tiers (%d) and atc_flags (%d) so stale entries "
                "from the prior code revision don't suppress new alerts.",
                sv,
                CURRENT_SCHEMA_VERSION,
                len(qt),
                len(af),
            )
            qt, af = {}, {}
        return cls(quantity_tiers=qt, atc_flags=af, schema_version=CURRENT_SCHEMA_VERSION)

    def save(self, location: Path | str) -> None:
        payload = {
            "quantity_tiers": self.quantity_tiers,
            "atc_flags": self.atc_flags,
            "schema_version": self.schema_version,
        }
        loc = str(location)
        if _is_redis_url(loc):
            _write_redis(loc, payload)
        else:
            _write_file(Path(loc), payload)

    # Quantity tier API
    def get_tier(self, product_title: str) -> int | None:
        return self.quantity_tiers.get(product_title)

    def set_tier(self, product_title: str, tier: int) -> None:
        self.quantity_tiers[product_title] = tier

    def clear_tier(self, product_title: str) -> None:
        self.quantity_tiers.pop(product_title, None)

    def crosses_lower_tier(self, product_title: str, new_tier: int) -> bool:
        """True if new_tier represents worse state than previously recorded."""
        prev = self.get_tier(product_title)
        if prev is None:
            return True
        return new_tier < prev

    # ATC flag API
    def is_new_atc_flag(self, key: str) -> bool:
        """True if the flag has never been recorded in any prior run."""
        return key not in self.atc_flags

    def should_post_atc_flag(self, key: str) -> bool:
        """True if the flag was observed in a prior run (persisted) AND has
        not yet been posted to Slack. This requires 2+ consecutive
        observations before posting — single-run crawler hydration blips
        (the dominant source of false positives) never get through."""
        entry = self.atc_flags.get(key)
        if entry is None:
            return False
        return not entry.get("posted_at")

    def mark_atc_flag(self, key: str, now: str) -> None:
        if key in self.atc_flags:
            self.atc_flags[key]["last_seen_at"] = now
        else:
            self.atc_flags[key] = {"first_seen_at": now, "last_seen_at": now}

    def mark_atc_flag_posted(self, key: str, now: str) -> None:
        """Record that this flag has been posted to Slack; prevents re-posting."""
        if key in self.atc_flags:
            self.atc_flags[key]["posted_at"] = now

    def retain_only_atc_flags(self, keep_keys: set[str]) -> None:
        """Drop ATC flags not in keep_keys (used after a full audit run)."""
        self.atc_flags = {k: v for k, v in self.atc_flags.items() if k in keep_keys}
