"""Fridays 9am PST: post full inventory snapshot to Slack.

Source of truth: ShipHero (Merchdrop warehouse). Resolves the
AUDIT_LAYOUT product names to ShipHero SKUs via the BundleRegistry's
substring-fallback name matcher.

Tracks 23 products across 6 categories at the trusted-single level.
Bundles excluded; their cover is pinned by lowest component (per the
weekend-merch report).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from based_inventory.config import Config
from based_inventory.discontinued import DiscontinuedFilter
from based_inventory.jobs._common import run_job
from based_inventory.registry import _name_matches, build_registry
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
ALIASES_PATH = DATA_DIR / "audit-aliases.json"


@dataclass
class ProductLine:
    name: str
    qty: int
    sku: str | None
    affected_bundles: list[str]


@dataclass(frozen=True)
class Resolved:
    """Result of resolving an AUDIT_LAYOUT name to ShipHero stock.

    `skus` lists every contributing physical SKU (single-element for
    direct matches; multi-element for aliased aggregates like
    "Tallow Moisturizer" = 50ml + 100ml). `qty` is the sum across
    those SKUs. `primary_sku` is the representative SKU shown in any
    UI that needs a single label (defaults to the first / largest).
    """

    primary_sku: str
    qty: int
    skus: tuple[str, ...]


def _load_aliases(path: Path) -> dict[str, dict[str, Any]]:
    """Read audit-aliases.json. Missing/invalid file = empty mapping (silent no-op)."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    aliases = raw.get("aliases") or {}
    return {k: v for k, v in aliases.items() if isinstance(v, dict)}


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
    by_sku: dict[str, WarehouseStock],
    bundle_skus: frozenset[str],
    discontinued: DiscontinuedFilter,
    aliases: dict[str, dict[str, Any]],
) -> Resolved | None:
    """Resolve an AUDIT_LAYOUT name to a Resolved stock summary.

    Resolution order:
    1. Explicit alias from `audit-aliases.json` — pin to a single SKU
       or aggregate across multiple SKUs. Aliases bypass kit / bundle /
       discontinued filters because Avi has explicitly chosen the SKU.
    2. Fuzzy `_name_matches` walk: iterate ranked candidates and return
       the first that passes is_kit / bundle / discontinued filters.
       Earlier behavior bailed to None as soon as the top match failed
       a filter (e.g. legacy V1 Scalp Scrubber mis-flagged is_kit=True
       killed the lookup even though V2 single was right behind it).

    Returns None only when the name has no alias AND every fuzzy
    candidate is filtered out.
    """
    alias = aliases.get(name)
    if alias:
        if "sku" in alias:
            stock = by_sku.get(alias["sku"])
            if stock is not None:
                return Resolved(primary_sku=stock.sku, qty=stock.on_hand, skus=(stock.sku,))
        if "skus" in alias:
            members = [by_sku[s] for s in alias["skus"] if s in by_sku]
            if members:
                # Pick highest-on_hand SKU as the primary label; sum the rest.
                primary = max(members, key=lambda s: s.on_hand)
                return Resolved(
                    primary_sku=primary.sku,
                    qty=sum(s.on_hand for s in members),
                    skus=tuple(s.sku for s in members),
                )
        # Alias present but its SKUs aren't in this warehouse: fall through
        # to fuzzy match rather than silently lying with qty=0.

    for candidate in _name_matches(name, by_name):
        if candidate.is_kit:
            continue
        if candidate.sku in bundle_skus:
            continue
        if discontinued.should_skip(candidate.sku, candidate.product_name):
            continue
        return Resolved(
            primary_sku=candidate.sku,
            qty=candidate.on_hand,
            skus=(candidate.sku,),
        )
    return None


def _affected_bundle_names(skus: tuple[str, ...], registry) -> list[str]:
    """Bundles whose components include any of `skus` (union across aggregates)."""
    skus_set = set(skus)
    out: list[str] = []
    for entry in registry.bundles:
        if any(c[0] in skus_set for c in entry.components_resolved):
            out.append(entry.bundle_name or entry.bundle_sku)
    return sorted(set(out))


def _run(cfg: Config) -> None:
    access_token = resolve_access_token(
        refresh_token=cfg.shiphero_refresh_token,
        fallback_access_token=cfg.shiphero_access_token,
    )
    client = ShipHeroClient(token=access_token, api_url=cfg.shiphero_api_url)
    discontinued = DiscontinuedFilter(DISCONTINUED_PATH)
    aliases = _load_aliases(ALIASES_PATH)
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

    # Pull aliased SKUs that aren't returned by warehouse_products (zero on_hand
    # rows can be excluded from the bulk paginated query). Without this, an
    # alias pinning to a 0-stock SKU would fall through to fuzzy match instead
    # of correctly reporting 0.
    aliased_skus: set[str] = set()
    for entry in aliases.values():
        if "sku" in entry:
            aliased_skus.add(entry["sku"])
        if "skus" in entry:
            aliased_skus.update(entry["skus"])
    for sku in sorted(aliased_skus - known):
        try:
            row = client.fetch_warehouse_product_for_sku(sku, MERCHDROP_WAREHOUSE_ID)
            if row is not None:
                stock.append(row)
        except RuntimeError:
            continue

    registry = build_registry(kits, stock, COMPONENTS_PATH)

    by_name: dict[str, list[WarehouseStock]] = {}
    by_sku: dict[str, WarehouseStock] = {}
    for s in stock:
        by_name.setdefault((s.product_name or "").strip(), []).append(s)
        by_sku.setdefault(s.sku, s)

    sections: list[tuple[str, list[ProductLine]]] = []
    for category, names in AUDIT_LAYOUT:
        lines: list[ProductLine] = []
        for name in names:
            resolved = _resolve_to_stock(
                name, by_name, by_sku, registry.bundle_skus, discontinued, aliases
            )
            if resolved is None:
                lines.append(ProductLine(name=name, qty=0, sku=None, affected_bundles=[]))
                continue
            lines.append(
                ProductLine(
                    name=name,
                    qty=resolved.qty,
                    sku=resolved.primary_sku,
                    affected_bundles=_affected_bundle_names(resolved.skus, registry),
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
