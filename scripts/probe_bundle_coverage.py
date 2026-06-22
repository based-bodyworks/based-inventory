"""One-off probe: are we capturing all curl cream bundle sales?

Read-only. No state mutation. Hits ShipHero API with the bot's existing
auth path. Outputs to stdout.

Run: python scripts/probe_bundle_coverage.py
(from ~/tools/based-inventory/, with .venv activated)

Answers:
1. How many kits does ShipHero have, split by kit_build true/false?
2. Which kits contain curl cream (BB-CC-SINGLE)? What's their kit_build flag?
3. What inventory_changes reason strings have appeared on BB-CC-SINGLE in
   the last 7 days, and which kit SKUs are actually firing rollup events?
4. Cross-check: total curl cream depletion via inventory_changes vs via
   orders+line_items aggregation for the same window. If they match,
   ShipHero rollups are complete. If orders > inventory_changes, the
   delta is the missed bundles.
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from based_inventory.shiphero import (  # noqa: E402
    MERCHDROP_WAREHOUSE_ID,
    ShipHeroClient,
)
from based_inventory.shiphero_auth import resolve_access_token  # noqa: E402

CURL_CREAM_SKU = "BB-CC-SINGLE"


def main() -> None:
    refresh = os.environ.get("SHIPHERO_REFRESH_TOKEN")
    fallback = os.environ.get("SHIPHERO_ACCESS_TOKEN")
    if not refresh and not fallback:
        sys.exit("ERROR: neither SHIPHERO_REFRESH_TOKEN nor SHIPHERO_ACCESS_TOKEN in env")

    token = resolve_access_token(refresh_token=refresh, fallback_access_token=fallback)
    client = ShipHeroClient(token=token)

    # ---------------------------------------------------------------- 1. KITS
    print("=" * 72)
    print("1. ALL SHIPHERO KITS")
    print("=" * 72)
    kits = client.fetch_all_kits()
    print(f"Total kits returned: {len(kits)}")
    kit_build_counts: Counter[bool] = Counter(k.is_kit_build for k in kits)
    print(f"  kit_build=True:  {kit_build_counts.get(True, 0)}")
    print(f"  kit_build=False: {kit_build_counts.get(False, 0)}")

    # ----------------------------------------------------- 2. CURL-CREAM KITS
    print()
    print("=" * 72)
    print(f"2. KITS CONTAINING {CURL_CREAM_SKU}")
    print("=" * 72)
    cc_kits = [k for k in kits if any(c[0] == CURL_CREAM_SKU for c in k.components)]
    print(f"{len(cc_kits)} kits contain {CURL_CREAM_SKU}\n")
    print(f"{'KIT_SKU':<35} {'BUILD':<6} {'NAME'}")
    print("-" * 72)
    for k in sorted(cc_kits, key=lambda x: (not x.is_kit_build, x.sku)):
        print(f"{k.sku:<35} {str(k.is_kit_build):<6} {k.name[:60]}")

    # ----------------------------- 3. INVENTORY_CHANGES REASON DISTRIBUTION
    print()
    print("=" * 72)
    print(f"3. INVENTORY_CHANGES REASONS FOR {CURL_CREAM_SKU} (last 7 days)")
    print("=" * 72)
    since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")

    query = """
    query($sku: String!, $warehouse_id: String!, $date_from: ISODateTime!) {
      inventory_changes(sku: $sku, warehouse_id: $warehouse_id, date_from: $date_from) {
        data {
          edges { node { id change_in_on_hand reason created_at } }
          pageInfo { hasNextPage }
        }
      }
    }
    """
    seen_ids: set[str] = set()
    reason_counts: Counter[str] = Counter()
    reason_units: Counter[str] = Counter()
    kit_sku_counts: Counter[str] = Counter()
    kit_sku_units: Counter[str] = Counter()
    direct_units = 0
    direct_count = 0
    other_units = 0
    other_count = 0
    cur_date_from = since
    pages = 0
    MAX_PAGES = 30  # safety cap

    while pages < MAX_PAGES:
        pages += 1
        payload = client._execute(
            query,
            {
                "sku": CURL_CREAM_SKU,
                "warehouse_id": MERCHDROP_WAREHOUSE_ID,
                "date_from": cur_date_from,
            },
        )
        edges = payload["data"]["inventory_changes"]["data"]["edges"]
        if not edges:
            break
        progress = False
        last_created = cur_date_from
        for e in edges:
            n = e["node"]
            evt_id = n.get("id") or ""
            if evt_id and evt_id in seen_ids:
                continue
            if evt_id:
                seen_ids.add(evt_id)
            progress = True
            reason = (n.get("reason") or "").strip()
            change = n.get("change_in_on_hand") or 0
            created = n.get("created_at") or last_created
            if created > last_created:
                last_created = created

            # Bucket by reason
            short = reason[:80]
            reason_counts[short] += 1
            if change < 0:
                reason_units[short] += -change

            # Categorize
            low = reason.lower()
            if "kit sku" in low and change < 0:
                # The reason embeds the kit SKU as an HTML anchor:
                # 'kit sku\n        <a href="/dashboard/products/detail/SKU_OR_ID">DISPLAY_SKU</a> was updated. Order #Y shipped.'
                # Extract the visible SKU between <a ...> and </a>.
                import re

                m = re.search(
                    r"kit sku\s*<a [^>]*>([^<]+)</a>", reason, re.IGNORECASE | re.DOTALL
                )
                if not m:
                    m = re.search(
                        r"kit sku[\s:]+([A-Za-z0-9\-_]+)", reason, re.IGNORECASE
                    )
                kit = m.group(1).strip() if m else "(unparsed)"
                kit_sku_counts[kit] += 1
                kit_sku_units[kit] += -change
            elif "order" in low and "shipped" in low and change < 0:
                direct_count += 1
                direct_units += -change
            elif change < 0:
                other_count += 1
                other_units += -change
                # Capture full reason for the "other" bucket (small N, valuable to see)
                if other_count <= 10:
                    print(f"  [OTHER #{other_count}] change={change} reason={reason[:300]!r}")

        if not progress:
            break
        if not payload["data"]["inventory_changes"]["data"]["pageInfo"]["hasNextPage"]:
            break
        if last_created == cur_date_from:
            break
        cur_date_from = last_created
        time.sleep(0.2)

    print(f"Pages fetched: {pages} (cap {MAX_PAGES})")
    print(f"Distinct events: {len(seen_ids)}")
    print()
    print("Distinct reason strings (top 10 by event count):")
    for reason, count in reason_counts.most_common(10):
        units = reason_units.get(reason, 0)
        print(f"  [{count:>4} events, {units:>5} units]  {reason!r}")
    print()
    print("Depletion breakdown:")
    print(f"  Direct 'Order ... shipped':       {direct_count:>4} events, {direct_units:>5} units")
    print(f"  Kit-rollup events (sum):           "
          f"{sum(kit_sku_counts.values()):>4} events, {sum(kit_sku_units.values()):>5} units")
    print(f"  Other (non-shipping reasons):     {other_count:>4} events, {other_units:>5} units")
    total_units = direct_units + sum(kit_sku_units.values())
    print(f"  TOTAL depletion (shipped + kits): {total_units:>5} units")
    print()
    print("Top kit SKUs firing rollup events on BB-CC-SINGLE (by units):")
    for kit_sku, units in kit_sku_units.most_common():
        # Cross-reference with the kit list to show the kit's name + kit_build flag
        match = next((k for k in kits if k.sku == kit_sku), None)
        if match:
            note = f"  (kit_build={match.is_kit_build}, name={match.name[:40]})"
        else:
            note = "  (NOT IN ShipHero kit registry — uncategorized)"
        print(f"  {kit_sku:<30} {kit_sku_units[kit_sku]:>5} units / {kit_sku_counts[kit_sku]:>3} events{note}")

    # Identify kits that contain curl cream but did NOT fire any rollup events
    fired = set(kit_sku_counts.keys())
    expected = {k.sku for k in cc_kits}
    silent = sorted(expected - fired)
    print()
    print(f"Curl-cream-containing kits that fired ZERO rollup events in window ({len(silent)} of {len(cc_kits)}):")
    for sku in silent:
        match = next((k for k in cc_kits if k.sku == sku), None)
        kb = match.is_kit_build if match else "?"
        nm = match.name[:50] if match else "?"
        print(f"  {sku:<30} kit_build={kb}  {nm}")

    # ---------------------- 4. CROSS-CHECK: ORDERS + LINE_ITEMS AGGREGATION
    print()
    print("=" * 72)
    print(f"4. CROSS-CHECK: ORDERS+LINE_ITEMS AGGREGATION (last 7 days)")
    print("=" * 72)
    print("Pulling 1 recent day (full 7d would exceed 4004-credit per-op cap)...")
    days = [(datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")]
    by_day_units: dict[str, int] = {}
    bundle_attribution: Counter[str] = Counter()
    for day in days:
        try:
            orders = client.fetch_orders_for_day(day, max_pages=20)
        except RuntimeError as e:
            print(f"  {day}: ERROR {e}")
            continue
        # Per-day: walk orders, expand bundle line items via the kit registry,
        # sum BB-CC-SINGLE depletion.
        kit_by_sku = {k.sku: k for k in kits}
        units = 0
        for order in orders:
            for li in (order.get("line_items") or {}).get("edges", []):
                n = li.get("node") or {}
                sku = n.get("sku")
                qty = int(n.get("quantity") or 0)
                if not sku or qty <= 0:
                    continue
                if sku == CURL_CREAM_SKU:
                    units += qty
                    bundle_attribution["(direct sale)"] += qty
                    continue
                kit = kit_by_sku.get(sku)
                if kit is None:
                    continue
                for comp_sku, comp_qty in kit.components:
                    if comp_sku == CURL_CREAM_SKU:
                        units += qty * comp_qty
                        bundle_attribution[sku] += qty * comp_qty
        by_day_units[day] = units
        print(f"  {day}: {len(orders)} orders, {units} curl cream units depleted")

    total_orders_units = sum(by_day_units.values())
    print()
    print(f"Total curl cream units (orders aggregation, 7d): {total_orders_units}")
    print(f"Total curl cream units (inventory_changes, 7d): {total_units}")
    delta = total_orders_units - total_units
    if total_orders_units > 0:
        pct = 100.0 * delta / total_orders_units
        print(f"Delta: {delta:+d} units ({pct:+.1f}% gap)")
    print()
    print("Top contributors via orders aggregation:")
    for sku, units in bundle_attribution.most_common(15):
        if sku == "(direct sale)":
            label = "(direct sale of BB-CC-SINGLE)"
        else:
            match = next((k for k in kits if k.sku == sku), None)
            label = f"(kit_build={match.is_kit_build}, name={match.name[:40]})" if match else "(unknown SKU)"
        print(f"  {sku:<30} {units:>5} units  {label}")


if __name__ == "__main__":
    main()
