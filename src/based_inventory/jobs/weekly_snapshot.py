"""Fridays 9am PST: post full inventory snapshot to Slack.

Source of truth: ShipHero (Merchdrop warehouse). Resolves the
AUDIT_LAYOUT product names to ShipHero SKUs via the BundleRegistry's
substring-fallback name matcher.

Tracks 23 products across 6 categories at the trusted-single level.
Bundles excluded; their cover is pinned by lowest component (per the
weekend-merch report).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from based_inventory.config import Config
from based_inventory.discontinued import DiscontinuedFilter
from based_inventory.jobs._common import run_job
from based_inventory.registry import _name_match, build_registry
from based_inventory.shiphero import MERCHDROP_WAREHOUSE_ID, ShipHeroClient, WarehouseStock
from based_inventory.shiphero_auth import resolve_access_token
from based_inventory.slack import SlackClient, context, divider, header, section

LOW = 1000

AUDIT_LAYOUT: list[tuple[str, list[str]]] = [
    ("Hair Care", ["Shampoo", "Conditioner", "Hair Elixir"]),
    ("Straight/Wavy Styling", ["Texture Powder", "Sea Salt Spray", "Pomade", "Hair Clay"]),
    (
        "Curly Styling",
        [
            "Leave-In Conditioner",
            "Curl Cream",
            "Curl Mousse",
            "Curl Gel",
            "Curl Refresh Spray",
        ],
    ),
    ("Body", ["Body Wash", "Body Lotion", "Deodorant"]),
    (
        "Skin",
        [
            "Daily Facial Cleanser",
            "Daily Facial Moisturizer",
            "Skin Revival Spray",
            "Under Eye Elixir",
            "Tallow Moisturizer",
        ],
    ),
    ("Accessories", ["Toiletry Bag", "Scalp Scrubber", "Wooden Hair Comb"]),
]

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
COMPONENTS_PATH = DATA_DIR / "set-components.json"
DISCONTINUED_PATH = DATA_DIR / "discontinued-skus.json"


@dataclass
class ProductLine:
    name: str
    qty: int
    sku: str | None
    affected_bundles: list[str]


def _emoji(qty: int) -> str:
    if qty < 0:
        return "⛔"
    if qty <= 100:
        return "🚨"
    if qty <= 500:
        return "🔴"
    if qty <= 750:
        return "🟠"
    if qty <= 1000:
        return "🟡"
    if qty <= 5000:
        return "📊"
    return "🟢"


def _render_line(line: ProductLine) -> str:
    if line.sku is None:
        return f"❓ {line.name}: not found in ShipHero"
    text = f"{_emoji(line.qty)} {line.name}: *{line.qty:,}*"
    if line.qty <= LOW and line.affected_bundles:
        preview = ", ".join(line.affected_bundles[:5])
        more = f" +{len(line.affected_bundles) - 5} more" if len(line.affected_bundles) > 5 else ""
        text += f" -> {preview}{more}"
    return text


def build_snapshot_blocks(
    sections: list[tuple[str, list[ProductLine]]], date_str: str
) -> list[dict[str, Any]]:
    total = sum(len(lines) for _, lines in sections)
    blocks: list[dict[str, Any]] = [
        header("📦 Weekly Inventory Audit"),
        section(
            f"Tracking *{total}* products at *single-SKU level* (ShipHero source of truth)\n"
            f"Bundles excluded; constrained by lowest component  |  🗓️ {date_str}"
        ),
        divider(),
    ]

    for category, lines in sections:
        body = "*" + category + "*\n" + "\n".join(_render_line(line) for line in lines)
        blocks.append(section(body))

    blocks.append(divider())
    blocks.append(
        context(
            "🟢 5K+  ·  📊 1K-5K  ·  🟡 ≤1K  ·  🟠 ≤750  ·  🔴 ≤500  ·  🚨 ≤100  ·  ⛔ Oversold  "
            "·  source: ShipHero (Merchdrop)"
        )
    )
    return blocks


def _resolve_to_stock(
    name: str,
    by_name: dict[str, list[WarehouseStock]],
    bundle_skus: frozenset[str],
    discontinued: DiscontinuedFilter,
) -> WarehouseStock | None:
    """Find the trusted-single WarehouseStock for an audit-layout name.

    Reuses BundleRegistry's _name_match which does exact / case-insensitive /
    substring fallback. Filters out kits, registry-known bundle SKUs, and
    discontinued / heuristic-match cruft.
    """
    match = _name_match(name, by_name)
    if match is None:
        return None
    if match.is_kit or match.sku in bundle_skus:
        return None
    if discontinued.should_skip(match.sku, match.product_name):
        return None
    return match


def _affected_bundle_names(sku: str, registry) -> list[str]:
    out: list[str] = []
    for entry in registry.bundles:
        if any(c[0] == sku for c in entry.components_resolved):
            out.append(entry.bundle_name or entry.bundle_sku)
    return sorted(set(out))


def _run(cfg: Config) -> None:
    access_token = resolve_access_token(
        refresh_token=cfg.shiphero_refresh_token,
        fallback_access_token=cfg.shiphero_access_token,
    )
    client = ShipHeroClient(token=access_token, api_url=cfg.shiphero_api_url)
    discontinued = DiscontinuedFilter(DISCONTINUED_PATH)
    slack = SlackClient(cfg.slack_bot_token, cfg.slack_channel, dry_run=cfg.dry_run)

    stock = client.fetch_warehouse_stock(warehouse_id=MERCHDROP_WAREHOUSE_ID)
    kits = client.fetch_all_kits()

    # Fill in component SKUs missing from page-1 fetch (so name match has them).
    component_skus = {c[0] for k in kits for c in k.components}
    known = {s.sku for s in stock}
    for sku in sorted(component_skus - known):
        try:
            row = client.fetch_warehouse_product_for_sku(sku, MERCHDROP_WAREHOUSE_ID)
            if row is not None:
                stock.append(row)
        except RuntimeError:
            continue

    registry = build_registry(kits, stock, COMPONENTS_PATH)

    # Index by product name for the resolver. When multiple SKUs share a
    # name (legacy + active), _name_match picks the highest on_hand.
    by_name: dict[str, list[WarehouseStock]] = {}
    for s in stock:
        by_name.setdefault((s.product_name or "").strip(), []).append(s)

    sections: list[tuple[str, list[ProductLine]]] = []
    for category, names in AUDIT_LAYOUT:
        lines: list[ProductLine] = []
        for name in names:
            match = _resolve_to_stock(name, by_name, registry.bundle_skus, discontinued)
            if match is None:
                lines.append(ProductLine(name=name, qty=0, sku=None, affected_bundles=[]))
                continue
            lines.append(
                ProductLine(
                    name=name,
                    qty=match.on_hand,
                    sku=match.sku,
                    affected_bundles=_affected_bundle_names(match.sku, registry),
                )
            )
        sections.append((category, lines))

    date_str = time.strftime("%b %d, %Y")
    blocks = build_snapshot_blocks(sections, date_str)
    fallback = f"📦 Weekly Inventory Audit: {date_str}"
    slack.post_message(fallback, blocks)


def main() -> None:
    run_job("weekly_snapshot", _run)


if __name__ == "__main__":
    main()
