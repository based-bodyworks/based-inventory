"""Daily 6am PST: crawl every public URL, diff ATC vs Shopify per variant.

Flow:
1. Pull Shopify products + variants with inventory + inventoryPolicy.
2. Enumerate every sitemap URL (PDPs, collections, pages, blog, homepage).
3. Index variants by (product_handle, variant_label) so each observation
   can be matched to its exact Shopify variant.
4. For each URL:
   - If it's a PDP, pass the product's variant labels to the crawler so
     it iterates the scent/pack picker and emits one observation per
     variant. Matches Shopify variant inventory 1:1.
   - Otherwise (collection card, landing page), emit whatever ATCs render
     in the default state and match them to the card's default variant.
5. Diff observed vs expected per variant; flag mismatches.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from based_inventory.auth import fetch_access_token
from based_inventory.config import Config
from based_inventory.crawl.atc import AtcCrawler, VariantObservation
from based_inventory.crawl.diff import ExpectedState, Flag, FlagType, generate_flags
from based_inventory.crawl.urls import UrlEnumerator
from based_inventory.jobs._common import run_job
from based_inventory.sets import SetResolver
from based_inventory.shopify import ShopifyClient
from based_inventory.skip_list import should_skip
from based_inventory.slack import SlackClient, context, divider, header, section
from based_inventory.state import AlertState

logger = logging.getLogger(__name__)

FLAG_ICONS = {
    FlagType.SALES_LEAK: "🛒",
    FlagType.OVERSELL_RISK: "⚠️",
    FlagType.NO_BUY_BUTTON: "👻",
}

FLAG_LABELS = {
    FlagType.SALES_LEAK: "SALES LEAK",
    FlagType.OVERSELL_RISK: "OVERSELL RISK",
    FlagType.NO_BUY_BUTTON: "NO BUY BUTTON",
}

V0_LIMITATION_FOOTER = (
    "⚠️ v0 limitation: trusts Shopify as source of truth. "
    "Shopify and ShipHero can drift. Verify against ShipHero before action."
)

COMPONENTS_PATH = Path(__file__).resolve().parents[3] / "data" / "set-components.json"


@dataclass(frozen=True)
class ExpectedVariant:
    """Per-variant expected state, keyed by (product_handle, normalized_label)."""

    variant_gid: str
    product_title: str
    variant_label: str
    expected: ExpectedState


@dataclass
class ExpectedProduct:
    """All variants for one product, indexed for observation matching."""

    product_handle: str
    product_title: str
    variants: list[ExpectedVariant] = field(default_factory=list)

    def variant_labels(self) -> list[str]:
        """Labels in Shopify order — passed to the crawler for picker iteration."""
        return [v.variant_label for v in self.variants]

    def find_by_label(self, label: str | None) -> ExpectedVariant | None:
        """Case-insensitive match on variant label. If label is None, return
        the variant most likely to render as the page's default."""
        if label is None:
            return self._default_variant()
        normalized = label.strip().lower()
        for v in self.variants:
            if v.variant_label.strip().lower() == normalized:
                return v
        # Partial match fallback (e.g. "Santal" vs "Santal Sandalwood / Just One")
        for v in self.variants:
            vl = v.variant_label.strip().lower()
            if normalized in vl or vl in normalized:
                return v
        return None

    def _default_variant(self) -> ExpectedVariant | None:
        """Pick the variant most likely to render by default on the PDP /
        in a collection card. Heuristic: prefer singles/default titles;
        otherwise the first variant Shopify returned."""
        for v in self.variants:
            t = v.variant_label.lower()
            if any(x in t for x in ("just one", "just ", "single")) or t in (
                "default title",
                "full size",
            ):
                return v
        return self.variants[0] if self.variants else None


def _sellable_from_variant(variant: dict[str, Any]) -> bool:
    levels = variant.get("inventoryItem", {}).get("inventoryLevels", [])
    return any(
        lvl["available"] > 0 and lvl["location"].get("shipsInventory", True) for lvl in levels
    )


def compute_expected_products(
    products: list[dict[str, Any]],
    set_resolver: SetResolver,  # kept for signature stability; no longer used
) -> dict[str, ExpectedProduct]:
    """Return product_handle -> ExpectedProduct (all variants).

    Per-variant sellable comes directly from the variant's Shopify
    inventoryLevels (any shipping location positive). This replaces the
    earlier singles-only set math, which was too coarse for multi-scent
    sets where the set product has 9 variants for 9 specific scent combos.
    """
    del set_resolver  # reserved for future use (e.g. bottleneck annotations)
    expected_by_handle: dict[str, ExpectedProduct] = {}

    for product in products:
        title = product["title"]
        handle = product.get("handle")
        if not handle:
            continue
        if should_skip(title):
            continue
        if title in ("Daily Facial Cleanser", "Daily Facial Moisturizer"):
            # No public PDP for these; they live as variants of Daily Skincare Duo.
            continue

        ep = ExpectedProduct(product_handle=handle, product_title=title)
        for variant in product.get("variants", []):
            policy = variant.get("inventoryPolicy", "DENY")
            ep.variants.append(
                ExpectedVariant(
                    variant_gid=variant["id"],
                    product_title=title,
                    variant_label=variant.get("title") or "",
                    expected=ExpectedState(
                        sellable=_sellable_from_variant(variant),
                        inventory_policy=policy,
                    ),
                )
            )
        if ep.variants:
            expected_by_handle[handle] = ep

    return expected_by_handle


def _dedupe_flags_by_state_key(flags: list[Flag]) -> list[Flag]:
    """Keep the first occurrence of each state_key."""
    seen: set[str] = set()
    out: list[Flag] = []
    for flag in flags:
        if flag.state_key in seen:
            continue
        seen.add(flag.state_key)
        out.append(flag)
    return out


def build_atc_blocks(flags: list[Flag]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        header(f"⚡ ATC Audit: {len(flags)} disagreement(s)"),
        divider(),
    ]

    order = {FlagType.OVERSELL_RISK: 0, FlagType.SALES_LEAK: 1, FlagType.NO_BUY_BUTTON: 2}
    sorted_flags = sorted(flags, key=lambda f: (order[f.flag_type], f.product_title))

    for f in sorted_flags:
        icon = FLAG_ICONS[f.flag_type]
        label = FLAG_LABELS[f.flag_type]
        variant_part = f" / {f.variant_label}" if f.variant_label else ""
        # Compact one-line format: link wraps the product title; Observed text
        # only appears when non-empty (NO BUY BUTTON has nothing meaningful to
        # quote, so omit). OVERSELL_RISK keeps the v0 limitation footer below.
        body = f"{icon} {label} — <{f.url}|*{f.product_title}*>{variant_part}"
        if f.observed_text:
            body += f' · "{f.observed_text}"'
        if f.flag_type == FlagType.OVERSELL_RISK:
            body += f"\n{V0_LIMITATION_FOOTER}"
        blocks.append(section(body))

    blocks.append(divider())

    ts = time.strftime("%b %d, %I:%M %p PST", time.gmtime(time.time() - 7 * 3600))
    blocks.append(context(f"🕐 {ts}"))
    return blocks


def _pdp_handle_from_url(url: str) -> str | None:
    """Return product handle if URL is /products/{handle}, else None."""
    marker = "/products/"
    if marker not in url:
        return None
    tail = url.split(marker, 1)[1]
    return tail.split("/")[0].split("?")[0] or None


def _flags_for_observation(
    obs: VariantObservation,
    expected_by_handle: dict[str, ExpectedProduct],
) -> list[Flag]:
    """Emit flags for a single observation against its matched Shopify variant."""
    if not obs.product_handle:
        return []

    product = expected_by_handle.get(obs.product_handle)
    if product is None:
        return []

    matched = product.find_by_label(obs.variant_label)
    if matched is None:
        return []

    return generate_flags(
        expected=matched.expected,
        observed=obs,
        variant_gid=matched.variant_gid,
        product_title=matched.product_title,
    )


def _run(cfg: Config) -> None:
    token = fetch_access_token(cfg.shopify_store, cfg.shopify_client_id, cfg.shopify_client_secret)
    shopify = ShopifyClient(cfg.shopify_store, token, cfg.shopify_api_version)
    set_resolver = SetResolver(COMPONENTS_PATH)
    state = AlertState.load(cfg.state_path)
    slack = SlackClient(cfg.slack_bot_token, cfg.slack_channel, dry_run=cfg.dry_run)
    url_enum = UrlEnumerator(f"https://{cfg.shopify_store.replace('.myshopify.com', '.com')}")

    logger.info("Fetching Shopify products")
    products = shopify.fetch_all_products()
    expected_by_handle = compute_expected_products(products, set_resolver)

    logger.info("Enumerating URLs")
    urls = url_enum.enumerate_all()
    logger.info(
        "Auditing %d URLs (pdp=%d, collection=%d, landing=%d, other=%d)",
        len(urls.all_urls),
        len(urls.pdp),
        len(urls.collection),
        len(urls.landing),
        len(urls.other),
    )

    all_flags: list[Flag] = []
    with AtcCrawler(headless=True) as crawler:
        for url in urls.all_urls:
            page_handle = _pdp_handle_from_url(url)
            variant_labels = None
            if page_handle and page_handle in expected_by_handle:
                variant_labels = expected_by_handle[page_handle].variant_labels()

            try:
                observations = crawler.audit_url(url, variant_labels=variant_labels)
            except Exception as exc:
                logger.warning("Crawl failed for %s: %s", url, exc)
                continue

            observed_handles_here: set[str] = set()
            for obs in observations:
                all_flags.extend(_flags_for_observation(obs, expected_by_handle))
                if obs.product_handle:
                    observed_handles_here.add(obs.product_handle)

            # If a PDP rendered but produced ZERO observations for its own
            # product, the theme is genuinely broken on that page.
            if (
                page_handle
                and page_handle in expected_by_handle
                and page_handle not in observed_handles_here
            ):
                product = expected_by_handle[page_handle]
                default = product.find_by_label(None)
                if default is not None:
                    all_flags.append(
                        Flag(
                            flag_type=FlagType.NO_BUY_BUTTON,
                            product_title=product.product_title,
                            variant_gid=default.variant_gid,
                            variant_label=None,
                            url=url,
                            expected_sellable=default.expected.sellable,
                            observed_text="",
                            state_key=f"{default.variant_gid}::{url}::NO_BUY_BUTTON",
                        )
                    )

    logger.info("Found %d flags", len(all_flags))

    now_iso = datetime.now(timezone.utc).isoformat()  # noqa: UP017

    # Post only flags that were also observed in the previous run. New
    # flags get recorded but not posted — this suppresses single-run
    # hydration-timing false positives, which have been the dominant
    # source of noise during the parallel-run period. A genuinely
    # persistent issue gets posted on its second consecutive observation
    # (max 1 day delay given the daily cron cadence).
    postable_flags = _dedupe_flags_by_state_key(
        [f for f in all_flags if state.should_post_atc_flag(f.state_key)]
    )

    state.retain_only_atc_flags({f.state_key for f in all_flags})
    for f in all_flags:
        state.mark_atc_flag(f.state_key, now=now_iso)
    # Note: mark_atc_flag_posted is intentionally deferred until AFTER
    # the Slack post and only runs when not in dry-run. Otherwise the
    # parallel-run period would silently consume flags — when DRY_RUN
    # flips to 0, any flag already marked posted would be skipped.
    state.save(cfg.state_path)

    logger.info(
        "Flag status: %d postable (persisted 2+ runs), %d total flagged this run",
        len(postable_flags),
        len(all_flags),
    )

    if not postable_flags:
        logger.info("No flags have persisted 2 consecutive runs; nothing to post")
        return

    blocks = build_atc_blocks(postable_flags)
    fallback = f"⚡ ATC Audit: {len(postable_flags)} new disagreement(s)"
    slack.post_message(fallback, blocks)

    if not cfg.dry_run:
        for f in postable_flags:
            state.mark_atc_flag_posted(f.state_key, now=now_iso)
        state.save(cfg.state_path)


def main() -> None:
    run_job("atc_audit", _run)


if __name__ == "__main__":
    main()
