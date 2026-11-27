from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from based_inventory.jobs import hourly_stock_watch as watch


def stock(sku, available, sell_ahead=0):
    return {
        "data": {
            "edges": [
                {
                    "node": {
                        "sku": sku,
                        "warehouse_id": watch.MERCHDROP_WAREHOUSE_ID,
                        "active": True,
                        "available": available,
                        "sell_ahead": sell_ahead,
                        "product": {"kit": False},
                    }
                }
            ],
            "pageInfo": {"hasNextPage": False},
        }
    }


def test_scope_uses_online_master_skus():
    catalog = watch.load_master_skus()

    assert catalog["BB-CC-SINGLE"] == "Curl Cream"
    assert catalog["BB-LEAVEIN-ONE"] == "Leave-In Conditioner"
    assert "BB-CC-SINGLE-RETAIL" not in catalog
    assert "BB-SSS-MINI-DUSK" not in catalog
    assert "BB-HRS-01" not in catalog


def test_stock_watch_flags_strictly_below_1000_and_sorts_units():
    client = Mock()
    client._execute.return_value = {
        "data": {
            "s0": stock("a", 999, 30_000),
            "s1": stock("b", 1_000, 70_000),
            "s2": stock("c", -2),
        }
    }
    catalog = {"a": "First", "b": "At threshold", "c": "Lowest"}

    result = watch.read_stock(client, catalog)
    message = watch.format_status(catalog, *result)

    assert "2 of 3 individual product SKUs are below 1,000 available units" in message
    assert "Sell-ahead shows how many units ShipHero lets us sell before they arrive." in message
    assert message.index("Lowest (c), -2 available") < message.index("First (a), 999 available")
    assert "First (a), 999 available | Sell-ahead active (30,000 units)" in message
    assert "Lowest (c), -2 available | No sell-ahead" in message
    assert "At threshold" not in message
    query, variables = client._execute.call_args.args
    assert "sell_ahead" in query
    assert variables == {"warehouse": watch.MERCHDROP_WAREHOUSE_ID, "s0": "a", "s1": "b", "s2": "c"}


def test_all_clear_includes_1000_units():
    assert "No individual product SKUs are below 1,000 available units" in watch.format_status(
        {"a": "One"}, {"a": watch.StockLevel(1_000, 70_000)}, []
    )


@pytest.mark.parametrize("sell_ahead", [None, True, -1, "30,000"])
def test_unverified_sell_ahead_preserves_confirmed_low_stock(sell_ahead):
    client = Mock()
    client._execute.return_value = {"data": {"s0": stock("a", 0, sell_ahead)}}
    catalog = {"a": "Low"}

    result = watch.read_stock(client, catalog)

    assert "Low (a), 0 available | Sell-ahead unverified" in watch.format_status(catalog, *result)


def test_graphql_error_retries_with_only_stock_fields():
    client = Mock()
    fallback_row = stock("a", 0)
    del fallback_row["data"]["edges"][0]["node"]["sell_ahead"]
    client._execute.side_effect = [
        RuntimeError("ShipHero GraphQL error"),
        {"data": {"s0": fallback_row}},
    ]

    levels, missing = watch.read_stock(client, {"a": "Low"})

    assert levels == {"a": watch.StockLevel(0, None)}
    assert missing == []
    assert "sell_ahead" in client._execute.call_args_list[0].args[0]
    assert "sell_ahead" not in client._execute.call_args_list[1].args[0]


@pytest.mark.parametrize("bad_field", ["available", "active", "warehouse_id", "sku", "product"])
def test_bad_row_never_becomes_an_all_clear(bad_field):
    bad_row = stock("a", 5_000)
    bad_row["data"]["edges"][0]["node"][bad_field] = None
    client = Mock()
    client._execute.return_value = {"data": {"s0": bad_row, "s1": stock("b", 999)}}
    catalog = {"a": "Unknown", "b": "Low"}

    counts, missing = watch.read_stock(client, catalog)
    message = watch.format_status(catalog, counts, missing)

    assert counts == {"b": watch.StockLevel(999, 0)}
    assert missing == ["a"]
    assert "Inventory check incomplete" in message
    assert "Unknown (a)" in message
    assert "Low (b), 999 available" in message
    assert "1 of the 1 checked SKUs is below 1,000 available units." in message
    assert "No individual product SKUs" not in message


def test_provider_failure_reports_missing_and_exits_with_error(monkeypatch, capsys):
    monkeypatch.setattr(watch, "load_master_skus", lambda: {"a": "One"})
    refresh = Mock(side_effect=RuntimeError("provider error detail"))
    monkeypatch.setattr(watch, "resolve_access_token", refresh)
    post = Mock()
    monkeypatch.setattr(watch, "SlackClient", post)

    assert watch.main([]) == 1
    assert "Inventory check incomplete" in capsys.readouterr().out
    post.assert_not_called()


def test_read_only_run_leaves_slack_alone(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(watch, "load_master_skus", lambda: {"a": "One"})
    monkeypatch.setattr(watch, "resolve_access_token", lambda *_: "test-token")
    monkeypatch.setattr(watch, "read_stock", lambda *_: ({"a": watch.StockLevel(1_000, 0)}, []))
    post = Mock()
    monkeypatch.setattr(watch, "SlackClient", post)

    assert watch.main(["--env-file", str(tmp_path / "unused.env")]) == 0
    assert "No individual product SKUs" in capsys.readouterr().out
    post.assert_not_called()


@pytest.mark.parametrize("dry_run", [None, "1", "0"])
def test_slack_requires_explicit_enablement(monkeypatch, dry_run):
    monkeypatch.setattr(watch, "load_master_skus", lambda: {"a": "One"})
    monkeypatch.setattr(watch, "resolve_access_token", lambda *_: "test-token")
    monkeypatch.setattr(watch, "read_stock", lambda *_: ({"a": watch.StockLevel(1_000, 0)}, []))
    if dry_run is None:
        monkeypatch.delenv("DRY_RUN", raising=False)
    else:
        monkeypatch.setenv("DRY_RUN", dry_run)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-slack-token")
    monkeypatch.setenv("SLACK_CHANNEL", "test-channel")
    monkeypatch.setenv("HOURLY_STOCK_DATABASE_URL", "postgresql://test")
    slack = Mock()
    publish = Mock(return_value=True)
    monkeypatch.setattr(watch, "SlackClient", slack)
    monkeypatch.setattr("based_inventory.jobs.hourly_stock_slack.publish_hourly_status", publish)

    assert watch.main(["--post-to-slack"]) == 0
    if dry_run == "0":
        slack.assert_called_once_with("test-slack-token", "test-channel")
        publish.assert_called_once()
    else:
        slack.assert_not_called()
        publish.assert_not_called()


def test_catalog_failure_reports_incomplete_in_slack_if_enabled(monkeypatch):
    monkeypatch.setattr(watch, "load_master_skus", Mock(side_effect=OSError()))
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-slack-token")
    monkeypatch.setenv("SLACK_CHANNEL", "test-channel")
    monkeypatch.setenv("HOURLY_STOCK_DATABASE_URL", "postgresql://test")
    slack = Mock()
    publish = Mock(return_value=True)
    monkeypatch.setattr(watch, "SlackClient", slack)
    monkeypatch.setattr("based_inventory.jobs.hourly_stock_slack.publish_hourly_status", publish)

    assert watch.main(["--post-to-slack"]) == 1
    _, _, status = publish.call_args.args
    assert "The list of individual product SKUs could not be read." in status


@pytest.mark.parametrize("start_hour, end_hour, expected", [(8, 9, 0), (7, 8, 1)])
def test_slack_delivery_uses_the_hour_the_check_started(
    monkeypatch, start_hour, end_hour, expected
):
    start = datetime(2026, 11, 27, start_hour, 59, tzinfo=UTC)
    end = datetime(2026, 11, 27, end_hour, tzinfo=UTC)
    monkeypatch.setattr(watch, "datetime", Mock(now=Mock(side_effect=[start, end])))
    monkeypatch.setattr(watch, "load_master_skus", lambda: {"a": "One"})
    monkeypatch.setattr(watch, "resolve_access_token", lambda *_: "test-token")
    monkeypatch.setattr(watch, "read_stock", lambda *_: ({"a": watch.StockLevel(1_000, 0)}, []))
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-slack-token")
    monkeypatch.setenv("SLACK_CHANNEL", "test-channel")
    monkeypatch.setenv("HOURLY_STOCK_DATABASE_URL", "postgresql://test")
    publish = Mock(return_value=True)
    monkeypatch.setattr("based_inventory.jobs.hourly_stock_slack.publish_hourly_status", publish)

    assert watch.main(["--post-to-slack"]) == expected
    if expected == 0:
        assert publish.call_args.kwargs["checked_at"] == start
    else:
        publish.assert_not_called()


def test_render_job_is_hourly_and_defaults_to_preview():
    config = Path("render.yaml").read_text(encoding="utf-8")
    service = config.split("    name: based-inventory-hourly\n", 1)[1].split("\n  - type:", 1)[0]

    assert 'schedule: "0 * * * *"' in service
    assert "dockerfilePath: ./Dockerfile.pipeline" in service
    assert "envVarKey: VELOCITY_DATABASE_URL" in service
    assert (
        "dockerCommand: python -m based_inventory.jobs.hourly_stock_watch --post-to-slack"
        in service
    )
    assert "- key: DRY_RUN\n        sync: false" in service
