import os
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from based_inventory.jobs.hourly_stock_slack import publish_hourly_status
from based_inventory.slack import SlackPostUncertainError

STATUS = (
    "ShipHero hourly stock check at the Merchdrop warehouse.\n"
    "No individual product SKUs are below 1,000 available units (40 checked)."
)


@pytest.fixture
def delivery():
    database_url = os.getenv("TEST_PIPELINE_DATABASE_URL")
    if not database_url:
        pytest.skip("requires a test PostgreSQL database")
    slack = Mock(channel=f"C-test-{uuid4()}")
    slack.post_with_receipt.side_effect = [
        "1795766400.000001",
        "1795770000.000002",
        "1795852800.000003",
    ]

    def publish(at):
        return publish_hourly_status(database_url, slack, STATUS, checked_at=at)

    return publish, slack


def test_one_root_per_pacific_date_and_one_delivery_per_hour(delivery):
    publish, slack = delivery

    assert publish(datetime(2026, 11, 27, 8, tzinfo=UTC))  # midnight PST
    assert publish(datetime(2026, 11, 27, 8, 20, tzinfo=UTC))  # retry
    assert publish(datetime(2026, 11, 27, 9, tzinfo=UTC))
    assert publish(datetime(2026, 11, 28, 8, tzinfo=UTC))

    start, reply, next_day = slack.post_with_receipt.call_args_list
    assert start.kwargs["thread_ts"] is None
    assert start.args[0].startswith("ShipHero stock tracker for November 27, 2026 (Pacific time).")
    assert (
        "This is today's first posted stock check for the Merchdrop warehouse. "
        "Later checks will be posted in this thread." in start.args[0]
    )
    assert reply.kwargs["thread_ts"] == "1795766400.000001"
    assert reply.args[0] == STATUS
    assert next_day.kwargs["thread_ts"] is None
    assert "November 28, 2026" in next_day.args[0]


def test_first_run_can_start_late_and_dst_repeat_gets_two_replies(delivery):
    publish, slack = delivery

    assert publish(datetime(2026, 11, 1, 7, 45, tzinfo=UTC))
    assert publish(datetime(2026, 11, 1, 8, tzinfo=UTC))  # 1 a.m. PDT
    assert publish(datetime(2026, 11, 1, 9, tzinfo=UTC))  # 1 a.m. PST

    assert [call.kwargs["thread_ts"] for call in slack.post_with_receipt.call_args_list] == [
        None,
        "1795766400.000001",
        "1795766400.000001",
    ]


def test_rejected_root_allows_first_successful_hour_to_start_thread(delivery):
    publish, slack = delivery
    slack.post_with_receipt.side_effect = [None, "1795770000.000002"]

    assert publish(datetime(2026, 11, 27, 8, tzinfo=UTC)) is False
    assert publish(datetime(2026, 11, 27, 9, tzinfo=UTC))
    assert all(call.kwargs["thread_ts"] is None for call in slack.post_with_receipt.call_args_list)


def test_uncertain_root_stops_duplicate_and_concurrent_posts(delivery):
    publish, slack = delivery
    later = datetime(2026, 11, 27, 9, tzinfo=UTC)

    def concurrent_send(*args, **kwargs):
        with pytest.raises(RuntimeError, match="unconfirmed"):
            publish(later)
        raise SlackPostUncertainError("post outcome unknown")

    slack.post_with_receipt.side_effect = concurrent_send
    with pytest.raises(SlackPostUncertainError):
        publish(datetime(2026, 11, 27, 8, tzinfo=UTC))
    with pytest.raises(RuntimeError, match="unconfirmed"):
        publish(later)
    slack.post_with_receipt.assert_called_once()


def test_uncertain_reply_blocks_its_hour_and_later_hours_keep_threading(delivery):
    publish, slack = delivery
    slack.post_with_receipt.side_effect = [
        "1795766400.000001",
        SlackPostUncertainError("post outcome unknown"),
        "1795773600.000003",
    ]
    assert publish(datetime(2026, 11, 27, 8, tzinfo=UTC))
    hour = datetime(2026, 11, 27, 9, tzinfo=UTC)
    with pytest.raises(SlackPostUncertainError):
        publish(hour)
    with pytest.raises(RuntimeError, match="unconfirmed"):
        publish(hour)
    assert publish(datetime(2026, 11, 27, 10, tzinfo=UTC))
    assert slack.post_with_receipt.call_args.kwargs["thread_ts"] == "1795766400.000001"
