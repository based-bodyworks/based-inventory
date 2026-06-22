"""Dump 5 raw kit-rollup reasons + 3 'other' reasons to see full text."""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from based_inventory.shiphero import MERCHDROP_WAREHOUSE_ID, ShipHeroClient  # noqa: E402
from based_inventory.shiphero_auth import resolve_access_token  # noqa: E402

SKU = "BB-CC-SINGLE"


def main() -> None:
    token = resolve_access_token(
        refresh_token=os.environ.get("SHIPHERO_REFRESH_TOKEN"),
        fallback_access_token=os.environ.get("SHIPHERO_ACCESS_TOKEN"),
    )
    client = ShipHeroClient(token=token)

    since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    query = """
    query($sku: String!, $warehouse_id: String!, $date_from: ISODateTime!) {
      inventory_changes(sku: $sku, warehouse_id: $warehouse_id, date_from: $date_from) {
        data { edges { node { id change_in_on_hand reason created_at user_id } } pageInfo { hasNextPage } }
      }
    }
    """
    seen: set[str] = set()
    rollup_samples = []
    other_samples = []
    direct_samples = []

    cur = since
    pages = 0
    while pages < 30:
        pages += 1
        payload = client._execute(
            query, {"sku": SKU, "warehouse_id": MERCHDROP_WAREHOUSE_ID, "date_from": cur}
        )
        edges = payload["data"]["inventory_changes"]["data"]["edges"]
        if not edges:
            break
        last = cur
        progress = False
        for e in edges:
            n = e["node"]
            eid = n.get("id") or ""
            if eid in seen:
                continue
            seen.add(eid)
            progress = True
            reason = n.get("reason") or ""
            change = n.get("change_in_on_hand") or 0
            created = n.get("created_at") or last
            if created > last:
                last = created
            low = reason.lower()
            if "kit sku" in low and len(rollup_samples) < 8:
                rollup_samples.append((change, reason, created))
            elif "order" in low and "shipped" in low and len(direct_samples) < 4:
                direct_samples.append((change, reason, created))
            elif change < 0 and len(other_samples) < 5:
                other_samples.append((change, reason, created))
        if not progress or last == cur:
            break
        if not payload["data"]["inventory_changes"]["data"]["pageInfo"]["hasNextPage"]:
            break
        cur = last
        time.sleep(0.2)

    print("=== KIT-ROLLUP RAW REASONS (8 samples) ===")
    for ch, r, t in rollup_samples:
        print(f"\n[{t}] change={ch}")
        print(repr(r))

    print("\n\n=== DIRECT SHIPPED RAW REASONS (4 samples) ===")
    for ch, r, t in direct_samples:
        print(f"\n[{t}] change={ch}")
        print(repr(r))

    print("\n\n=== OTHER RAW REASONS (all) ===")
    for ch, r, t in other_samples:
        print(f"\n[{t}] change={ch}")
        print(repr(r))


if __name__ == "__main__":
    main()
