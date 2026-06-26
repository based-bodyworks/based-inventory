"""One-off: list ShipHero SKUs that fuzzy-match Body Wash / Body Lotion /
Deodorant so we can pick the right alias targets.

Reproduces the resolver's tier-3 substring matching to show which SKUs the
weekly snapshot is currently picking up. The 2026-05-09 Slack post had
'Body Wash: 25,000' which Avi flagged as `BW-SNTL-PK001` (Container, Cap,
Body Wash) — a packaging component, not the actual product.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from based_inventory.shiphero import MERCHDROP_WAREHOUSE_ID, ShipHeroClient  # noqa: E402
from based_inventory.shiphero_auth import resolve_access_token  # noqa: E402

NAMES = ["Body Wash", "Body Lotion", "Deodorant"]


def main() -> None:
    token = resolve_access_token(
        refresh_token=os.environ.get("SHIPHERO_REFRESH_TOKEN"),
        fallback_access_token=os.environ.get("SHIPHERO_ACCESS_TOKEN"),
    )
    client = ShipHeroClient(token=token)
    stock = client.fetch_warehouse_stock(warehouse_id=MERCHDROP_WAREHOUSE_ID)

    for needle in NAMES:
        print("=" * 80)
        print(f"Substring matches for {needle!r}")
        print("=" * 80)
        lowered = needle.lower()
        hits = []
        for s in stock:
            name = (s.product_name or "").strip()
            if lowered in name.lower():
                hits.append(s)
        # Sort by on_hand desc to show the resolver's preferred candidate first
        hits.sort(key=lambda s: -s.on_hand)
        for s in hits:
            kind = "KIT" if s.is_kit else "single"
            print(
                f"  {s.sku:<32} {kind:<6}  on_hand={s.on_hand:>7,}  "
                f"available={s.available:>7,}  backorder={s.backorder:>6,}  "
                f"{s.product_name[:50]}"
            )
        print(f"  ({len(hits)} candidate(s))")
        print()


if __name__ == "__main__":
    main()
