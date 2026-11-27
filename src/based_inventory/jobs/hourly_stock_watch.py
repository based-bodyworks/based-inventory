"""Report current availability for the online master SKUs at Merchdrop."""

from __future__ import annotations

import argparse
import json
import logging
import os
from importlib.resources import files
from pathlib import Path

from dotenv import dotenv_values

from based_inventory.discontinued import DiscontinuedFilter
from based_inventory.shiphero import MERCHDROP_WAREHOUSE_ID, ShipHeroClient
from based_inventory.shiphero_auth import resolve_access_token
from based_inventory.slack import SlackClient

logger = logging.getLogger(__name__)
THRESHOLD = 1_000
SKUS_PER_QUERY = 8
STATUS_HEADING = "ShipHero hourly stock check at the Merchdrop warehouse."
DISCONTINUED_PATH = Path(__file__).resolve().parents[3] / "data" / "discontinued-skus.json"


def load_master_skus() -> dict[str, str]:
    source = files("based_inventory.pipeline.cloud_helpers").joinpath("inventory_catalog.json")
    catalog = json.loads(source.read_text(encoding="utf-8"))
    discontinued = DiscontinuedFilter(DISCONTINUED_PATH)
    masters = {}
    seen = set()
    for item in catalog:
        sku, name = item["sku"], item["product"]
        if not sku or not name or sku in seen:
            raise ValueError("The inventory catalog has a blank or duplicate SKU")
        seen.add(sku)
        if not item.get("online_cover_note") and not discontinued.should_skip(sku, name):
            masters[sku] = name
    if not masters:
        raise ValueError("The inventory catalog has no online master SKUs")
    return masters


def read_stock(client: ShipHeroClient, catalog: dict[str, str]) -> tuple[dict[str, int], list[str]]:
    counts = {}
    skus = list(catalog)
    for offset in range(0, len(skus), SKUS_PER_QUERY):
        batch = {f"s{i}": sku for i, sku in enumerate(skus[offset : offset + SKUS_PER_QUERY])}
        parameters = ", ".join(f"${alias}: String!" for alias in batch)
        selections = " ".join(
            f"{alias}: warehouse_products(sku: ${alias}, warehouse_id: $warehouse)"
            " {data {edges {node {...Stock}} pageInfo {hasNextPage}}}"
            for alias in batch
        )
        query = (
            f"query($warehouse: String!, {parameters}) {{ {selections} }}"
            " fragment Stock on WarehouseProduct {sku warehouse_id active available product {kit}}"
        )
        try:
            response = client._execute(
                query, {"warehouse": MERCHDROP_WAREHOUSE_ID, **batch}, retries=2
            )
        except RuntimeError as exc:
            logger.warning("ShipHero stock read stopped: %s", exc)
            break
        for alias, sku in batch.items():
            try:
                data = response["data"][alias]["data"]
                edges = data["edges"]
                if data["pageInfo"]["hasNextPage"] is not False or len(edges) != 1:
                    continue
                row = edges[0]["node"]
                if (
                    row["sku"] == sku
                    and row["warehouse_id"] == MERCHDROP_WAREHOUSE_ID
                    and row["active"] is True
                    and row["product"]["kit"] is False
                    and type(row["available"]) is int
                ):
                    counts[sku] = row["available"]
            except (KeyError, TypeError):
                logger.warning("ShipHero returned an incomplete stock row for %s", sku)
    return counts, [sku for sku in skus if sku not in counts]


def format_status(catalog: dict[str, str], counts: dict[str, int], missing: list[str]) -> str:
    lines = [STATUS_HEADING]
    low = sorted(
        (sku for sku, units in counts.items() if units < THRESHOLD),
        key=lambda sku: (counts[sku], catalog[sku], sku),
    )
    if missing:
        lines.append(
            f"Inventory check incomplete. Verified {len(counts)} of {len(catalog)} individual product SKUs."
        )
        if counts:
            lines.extend(f"Could not verify {catalog[sku]} ({sku})." for sku in missing)
    elif not low:
        lines.append(
            f"No individual product SKUs are below {THRESHOLD:,} available units ({len(counts)} checked)."
        )
    if low:
        verb = "is" if len(low) == 1 else "are"
        scope = (
            f"the {len(counts)} checked SKUs"
            if missing
            else f"{len(counts)} individual product SKUs"
        )
        lines.append(f"{len(low)} of {scope} {verb} below {THRESHOLD:,} available units.")
        lines.extend(f"• {catalog[sku]} ({sku}), {counts[sku]:,} available" for sku in low)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path, help="Read credentials from an existing dotenv file"
    )
    parser.add_argument("--post-to-slack", action="store_true", help="Post when DRY_RUN is 0")
    options = parser.parse_args(argv)
    settings = {**(dotenv_values(options.env_file) if options.env_file else {}), **os.environ}
    try:
        catalog = load_master_skus()
    except (OSError, ValueError, TypeError, KeyError):
        incomplete = True
        status = (
            f"{STATUS_HEADING}\n"
            "Inventory check incomplete. The list of individual product SKUs could not be read."
        )
    else:
        try:
            token = resolve_access_token(
                settings.get("SHIPHERO_REFRESH_TOKEN"), settings.get("SHIPHERO_ACCESS_TOKEN")
            )
            client = ShipHeroClient(
                token,
                settings.get("SHIPHERO_API_URL") or "https://public-api.shiphero.com/graphql",
                min_credits_floor=1_800,
            )
            counts, missing = read_stock(client, catalog)
        except RuntimeError as exc:
            logger.warning("ShipHero stock check could not start: %s", exc)
            counts, missing = {}, list(catalog)
        incomplete = bool(missing)
        status = format_status(catalog, counts, missing)
    print(status)

    if options.post_to_slack and settings.get("DRY_RUN") == "0":
        slack_token = settings.get("SLACK_BOT_TOKEN")
        channel = settings.get("SLACK_CHANNEL")
        if not slack_token or not channel:
            logger.error("Missing Slack credentials or destination")
            return 1
        lines = status.splitlines()
        blocks = [
            {
                "type": "section",
                "text": {"type": "plain_text", "text": "\n".join(lines[i : i + 12])},
            }
            for i in range(0, len(lines), 12)
        ]
        if not SlackClient(slack_token, channel).post_message(status, blocks):
            return 1
    return int(incomplete)


if __name__ == "__main__":
    raise SystemExit(main())
