"""One-off probe: enumerate Based's Amazon FBA inventory via SP-API.

Read-only. Pulls every FBA SKU + ASIN with the full breakdown
(fulfillable / inbound / reserved / unsellable / researching) and dumps
to stdout + JSON. Use this to:

1. Validate that the SP-API auth + roles are configured correctly.
2. See the actual ASIN list — needed to build the SKU mapping between
   Amazon listings and Merchdrop physical SKUs.
3. Compare against ShipHero's `fba_inventory` (which only exposed
   `quantity` for ~2 of 11 hero SKUs in the 2026-05-07 probe).

Run: python scripts/probe_amazon_fba.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from based_inventory.amazon import US_MARKETPLACE_ID, AmazonSPClient  # noqa: E402
from based_inventory.amazon_auth import fetch_access_token  # noqa: E402

OUTPUT_PATH = ROOT / "data" / "_probe-amazon-fba.json"


def main() -> None:
    client_id = os.environ.get("AMAZON_LWA_CLIENT_ID")
    client_secret = os.environ.get("AMAZON_LWA_CLIENT_SECRET")
    refresh = os.environ.get("AMAZON_REFRESH_TOKEN")
    marketplace = os.environ.get("AMAZON_MARKETPLACE_ID", US_MARKETPLACE_ID)

    if not client_id or not client_secret or not refresh:
        sys.exit(
            "ERROR: missing one of AMAZON_LWA_CLIENT_ID, "
            "AMAZON_LWA_CLIENT_SECRET, AMAZON_REFRESH_TOKEN. "
            "Set them in .env (see .env.example)."
        )

    print("[1/3] Exchanging refresh token for access token via LWA...")
    access = fetch_access_token(refresh, client_id, client_secret)
    print(f"  Got access token (length {len(access)} chars). Expires in 1h.")

    print(f"\n[2/3] Pulling FBA inventory summaries for marketplace {marketplace}...")
    client = AmazonSPClient(access_token=access, marketplace_id=marketplace)
    summaries = client.fetch_fba_inventory_summaries()
    print(f"  Got {len(summaries)} inventory rows.")

    print("\n[3/3] Summary by SKU:")
    print(
        f"  {'SELLER_SKU':<32} {'ASIN':<14} {'FULFILL':>7}  {'INBOUND':>7}  "
        f"{'RESERVED':>8}  {'UNSELL':>6}  NAME"
    )
    print("  " + "-" * 110)
    total_fulfillable = 0
    total_inbound = 0
    total_unsellable = 0
    for s in sorted(summaries, key=lambda x: -x.fulfillable):
        inbound = s.inbound_working + s.inbound_shipped + s.inbound_receiving
        total_fulfillable += s.fulfillable
        total_inbound += inbound
        total_unsellable += s.unsellable_total
        print(
            f"  {s.seller_sku[:32]:<32} {s.asin:<14} "
            f"{s.fulfillable:>7,}  {inbound:>7,}  "
            f"{s.reserved_total:>8,}  {s.unsellable_total:>6,}  {s.product_name[:40]}"
        )
    print("  " + "-" * 110)
    print(
        f"  {'TOTAL':<48} {total_fulfillable:>7,}  "
        f"{total_inbound:>7,}  {'':>8}  {total_unsellable:>6,}"
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps([s.raw for s in summaries], indent=2, default=str)
    )
    print(f"\nFull raw responses dumped to {OUTPUT_PATH}")
    print("\nNext: cross-reference seller_sku values against ShipHero physical SKUs.")
    print("  - SKUs that match -> direct FBA listing of a single component")
    print("  - SKUs that DON'T match -> Amazon-specific multi-pack / bundle (needs mapping)")


if __name__ == "__main__":
    main()
