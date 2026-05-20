"""Tests for persistent alert state."""

from pathlib import Path
from unittest.mock import patch

import fakeredis

from based_inventory.state import REDIS_STATE_KEY, AlertState


def test_load_missing_file_returns_empty(tmp_path: Path):
    state = AlertState.load(tmp_path / "missing.json")
    assert state.quantity_tiers == {}
    assert state.atc_flags == {}


def test_load_malformed_file_returns_empty(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    state = AlertState.load(path)
    assert state.quantity_tiers == {}


def test_set_and_get_quantity_tier(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_tier("Shampoo", 500)
    assert state.get_tier("Shampoo") == 500
    assert state.get_tier("Unknown") is None


def test_crosses_lower_tier_true_on_drop(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_tier("Shampoo", 1000)
    assert state.crosses_lower_tier("Shampoo", 500) is True


def test_crosses_lower_tier_false_on_same(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_tier("Shampoo", 500)
    assert state.crosses_lower_tier("Shampoo", 500) is False


def test_crosses_lower_tier_false_on_recovery(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_tier("Shampoo", 500)
    assert state.crosses_lower_tier("Shampoo", 1000) is False


def test_first_time_ever_crosses_true(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    assert state.crosses_lower_tier("NewProduct", 500) is True


def test_atc_flag_new(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    key = "gid://shopify/ProductVariant/1::/products/x::SALES_LEAK"
    assert state.is_new_atc_flag(key) is True
    state.mark_atc_flag(key, now="2026-04-15T06:00:00Z")
    assert state.is_new_atc_flag(key) is False


def test_save_and_reload(tmp_path: Path):
    path = tmp_path / "s.json"
    state = AlertState.load(path)
    state.set_tier("A", 100)
    state.mark_atc_flag("k1", now="2026-04-15T06:00:00Z")
    state.save(path)

    reloaded = AlertState.load(path)
    assert reloaded.get_tier("A") == 100
    assert reloaded.is_new_atc_flag("k1") is False


def test_load_wrong_shape_returns_empty(tmp_path: Path):
    path = tmp_path / "wrong.json"
    path.write_text("[1, 2, 3]")
    state = AlertState.load(path)
    assert state.quantity_tiers == {}
    assert state.atc_flags == {}


def test_load_wrong_value_types_returns_empty(tmp_path: Path):
    path = tmp_path / "weird.json"
    path.write_text('{"quantity_tiers": "oops", "atc_flags": [1,2]}')
    state = AlertState.load(path)
    assert state.quantity_tiers == {}
    assert state.atc_flags == {}


def test_should_post_atc_flag_posts_first_observation(tmp_path: Path):
    """A flag posts on first observation under the weekly cadence
    (post-2026-04-30). The 2-run persistence requirement was dropped
    when the cron moved from daily to weekly — waiting 2 weeks on a
    real ATC outage was unacceptable."""
    state = AlertState.load(tmp_path / "s.json")
    key = "gid://shopify/ProductVariant/1::/products/x::SALES_LEAK"

    # Run 1: first observation. Postable immediately.
    assert state.should_post_atc_flag(key) is True
    state.mark_atc_flag(key, now="2026-04-20T06:00:00Z")
    # Still postable until we mark it posted.
    assert state.should_post_atc_flag(key) is True

    # Mark it posted (as audit does after building the Slack blocks).
    state.mark_atc_flag_posted(key, now="2026-04-20T06:01:00Z")
    assert state.should_post_atc_flag(key) is False  # already posted, don't re-post


def test_should_post_atc_flag_does_not_repost_same_break(tmp_path: Path):
    """A flag that's already been posted should not post again on the
    next run, even if the underlying break is still present."""
    state = AlertState.load(tmp_path / "s.json")
    key = "gid://shopify/ProductVariant/1::/products/x::NO_BUY_BUTTON"

    # Run 1: observe + post.
    state.mark_atc_flag(key, now="2026-04-20T06:00:00Z")
    state.mark_atc_flag_posted(key, now="2026-04-20T06:01:00Z")
    state.save(tmp_path / "s.json")

    # Run 2 (next Monday): same break still present. retain keeps it,
    # mark refreshes last_seen, but should_post returns False because
    # posted_at is set.
    state2 = AlertState.load(tmp_path / "s.json")
    state2.retain_only_atc_flags({key})
    state2.mark_atc_flag(key, now="2026-04-27T06:00:00Z")
    assert state2.should_post_atc_flag(key) is False


def test_clear_atc_flags_not_in_set(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.mark_atc_flag("k1", now="2026-04-15T06:00:00Z")
    state.mark_atc_flag("k2", now="2026-04-15T06:00:00Z")

    state.retain_only_atc_flags({"k1"})

    assert state.is_new_atc_flag("k1") is False
    assert state.is_new_atc_flag("k2") is True


# Redis backend tests


def _fake_redis_patch():
    """Patch redis.from_url to return a fakeredis client."""
    server = fakeredis.FakeServer()

    def _factory(url, *args, **kwargs):
        del url, args
        return fakeredis.FakeRedis(
            server=server, decode_responses=kwargs.get("decode_responses", False)
        )

    return patch("redis.from_url", side_effect=_factory), server


def test_redis_load_missing_key_returns_empty():
    patcher, _server = _fake_redis_patch()
    with patcher:
        state = AlertState.load("redis://localhost:6379/0")
    assert state.quantity_tiers == {}
    assert state.atc_flags == {}


def test_redis_save_and_reload():
    patcher, server = _fake_redis_patch()
    with patcher:
        state = AlertState.load("redis://localhost:6379/0")
        state.set_tier("Shampoo", 500)
        state.mark_atc_flag("k1", now="2026-04-15T06:00:00Z")
        state.save("redis://localhost:6379/0")

        reloaded = AlertState.load("redis://localhost:6379/0")

    assert reloaded.get_tier("Shampoo") == 500
    assert reloaded.is_new_atc_flag("k1") is False

    # Verify payload shape in Redis directly
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    raw = client.get(REDIS_STATE_KEY)
    assert raw is not None
    assert '"quantity_tiers"' in raw
    assert '"atc_flags"' in raw


def test_redis_malformed_json_returns_empty():
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    client.set(REDIS_STATE_KEY, "not valid json")

    def _factory(url, *args, **kwargs):
        del url, args
        return fakeredis.FakeRedis(
            server=server, decode_responses=kwargs.get("decode_responses", False)
        )

    with patch("redis.from_url", side_effect=_factory):
        state = AlertState.load("redis://localhost:6379/0")

    assert state.quantity_tiers == {}
    assert state.atc_flags == {}


def test_redis_url_with_tls_scheme_dispatches_to_redis():
    patcher, _server = _fake_redis_patch()
    with patcher:
        state = AlertState.load("rediss://localhost:6379/0")
    assert state.quantity_tiers == {}


# --------------------------------------------------------------------------
# Backorder tier API (added 2026-05-20 alongside the dual-ladder fix).
# Direction-of-worse is opposite to quantity_tiers: higher value = worse.
# --------------------------------------------------------------------------


def test_set_and_get_backorder_tier(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_backorder_tier("CLAY1", 10_000)
    assert state.get_backorder_tier("CLAY1") == 10_000
    assert state.get_backorder_tier("UnknownSKU") is None


def test_crosses_higher_backorder_tier_first_time_true(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    assert state.crosses_higher_backorder_tier("CLAY1", 1_000) is True


def test_crosses_higher_backorder_tier_true_on_escalation(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_backorder_tier("CLAY1", 1_000)
    assert state.crosses_higher_backorder_tier("CLAY1", 5_000) is True
    assert state.crosses_higher_backorder_tier("CLAY1", 10_000) is True


def test_crosses_higher_backorder_tier_false_on_same(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_backorder_tier("CLAY1", 5_000)
    assert state.crosses_higher_backorder_tier("CLAY1", 5_000) is False


def test_crosses_higher_backorder_tier_false_on_recovery(tmp_path: Path):
    # Backorder bucket dropping is good news — don't re-alert.
    state = AlertState.load(tmp_path / "s.json")
    state.set_backorder_tier("CLAY1", 10_000)
    assert state.crosses_higher_backorder_tier("CLAY1", 5_000) is False
    assert state.crosses_higher_backorder_tier("CLAY1", 1_000) is False


def test_clear_backorder_tier(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_backorder_tier("CLAY1", 1_000)
    state.clear_backorder_tier("CLAY1")
    assert state.get_backorder_tier("CLAY1") is None
    # Clearing a missing key is a no-op, not an error.
    state.clear_backorder_tier("NeverSet")


def test_backorder_tiers_round_trip_through_save_and_reload(tmp_path: Path):
    path = tmp_path / "s.json"
    state = AlertState.load(path)
    state.set_tier("BB-SHMP", 500)
    state.set_backorder_tier("CLAY1", 10_000)
    state.set_backorder_tier("BB-CC-01", 1_000)
    state.save(path)

    reloaded = AlertState.load(path)
    assert reloaded.get_tier("BB-SHMP") == 500
    assert reloaded.get_backorder_tier("CLAY1") == 10_000
    assert reloaded.get_backorder_tier("BB-CC-01") == 1_000


def test_schema_v2_state_is_cleared_on_load_under_v3(tmp_path: Path):
    """Regression: an old v2 payload had quantity_tiers keyed off on_hand-derived
    tier values. Reloading that under v3 (which interprets tier values as
    available-derived) would suppress legitimate new alerts. The schema_version
    guard MUST wipe both quantity_tiers and backorder_tiers on mismatch so the
    next run re-evaluates from scratch."""
    path = tmp_path / "s.json"
    path.write_text(
        '{"schema_version": "v2", "quantity_tiers": {"CLAY1": 100}, '
        '"backorder_tiers": {"CLAY1": 5000}, "atc_flags": {}}'
    )
    state = AlertState.load(path)
    assert state.quantity_tiers == {}
    assert state.backorder_tiers == {}
    assert state.schema_version == "v3"


def test_load_treats_missing_backorder_tiers_as_empty(tmp_path: Path):
    # Pre-v3 payloads have no backorder_tiers key — handle gracefully.
    path = tmp_path / "s.json"
    path.write_text('{"schema_version": "v3", "quantity_tiers": {}, "atc_flags": {}}')
    state = AlertState.load(path)
    assert state.backorder_tiers == {}


def test_load_treats_non_object_backorder_tiers_as_empty(tmp_path: Path):
    path = tmp_path / "s.json"
    path.write_text(
        '{"schema_version": "v3", "quantity_tiers": {}, '
        '"backorder_tiers": "oops", "atc_flags": {}}'
    )
    state = AlertState.load(path)
    assert state.backorder_tiers == {}
