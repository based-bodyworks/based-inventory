"""Probe ShipHero's Product.fba_inventory field shape + current values.

Read-only. Runs against the live Based account. Outputs to stdout.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from based_inventory.shiphero import ShipHeroClient  # noqa: E402
from based_inventory.shiphero_auth import resolve_access_token  # noqa: E402

HERO_SKUS = [
    "BB-CC-SINGLE",
    "44114434293989",  # Shampoo
    "44114437669093",  # Conditioner
    "44126606262501",  # Texture Powder
    "BB-LEAVEIN-ONE",
    "BB2000S-1-6-1",  # Sea Salt Spray
    "BB-UEE-SINGLE",
    "BB-SRS-4OZ",
    "BB-MOUS-SINGLE",
    "POMADE1",
    "CLAY1",
]


def main() -> None:
    token = resolve_access_token(
        refresh_token=os.environ.get("SHIPHERO_REFRESH_TOKEN"),
        fallback_access_token=os.environ.get("SHIPHERO_ACCESS_TOKEN"),
    )
    client = ShipHeroClient(token=token)

    # ----- 1. Schema introspection on Product type
    print("=" * 72)
    print("1. INTROSPECTION: Product.fba_inventory field shape")
    print("=" * 72)
    introspect = """
    query {
      __type(name: "Product") {
        fields {
          name
          type { name kind ofType { name kind } }
        }
      }
    }
    """
    try:
        payload = client._execute(introspect)
        fields = payload["data"]["__type"]["fields"]
        for f in fields:
            n = f["name"]
            if (
                "fba" in n.lower()
                or "inventory" in n.lower()
                or "amazon" in n.lower()
                or "kit" in n.lower()
            ):
                print(f"  {n}: {f['type']}")
    except Exception as e:
        print(f"  introspection error: {e}")

    # ----- 2. fba_inventory field shape
    print()
    print("=" * 72)
    print("2. INTROSPECTION: FbaInventory type fields")
    print("=" * 72)
    for type_name in ("FbaInventory", "FbaInventoryConnection", "FbaInventoryNode"):
        try:
            q = """query($n: String!) { __type(name: $n) { name kind fields { name type { name kind ofType { name kind } } } } }"""
            payload = client._execute(q, {"n": type_name})
            t = payload["data"]["__type"]
            if t:
                print(f"\n  Type: {type_name} ({t.get('kind')})")
                for f in t.get("fields") or []:
                    print(f"    {f['name']}: {f['type']}")
        except Exception as e:
            print(f"  {type_name} error: {e}")

    # ----- 3. Real values for hero SKUs
    print()
    print("=" * 72)
    print("3. fba_inventory VALUES for hero SKUs")
    print("=" * 72)
    query = """
    query($sku: String!) {
      products(sku: $sku) {
        data {
          edges {
            node {
              id
              sku
              name
              kit
              fba_inventory {
                id
                legacy_id
                quantity
                marketplace_id
                merchant_id
              }
            }
          }
        }
      }
    }
    """
    for sku in HERO_SKUS:
        try:
            payload = client._execute(query, {"sku": sku})
            edges = payload["data"]["products"]["data"]["edges"]
            if not edges:
                print(f"\n  {sku}: NOT FOUND in products()")
                continue
            for edge in edges:
                n = edge["node"]
                fba = n.get("fba_inventory")
                if not fba:
                    print(f"\n  {sku} ({n.get('name','')[:40]}): fba_inventory = null/empty")
                    continue
                if isinstance(fba, list):
                    print(f"\n  {sku} ({n.get('name','')[:40]}): {len(fba)} fba_inventory rows")
                    for row in fba:
                        print(f"    {json.dumps(row, indent=6, default=str)}")
                else:
                    print(
                        f"\n  {sku} ({n.get('name','')[:40]}): {json.dumps(fba, indent=4, default=str)}"
                    )
        except Exception as e:
            err = str(e)
            print(f"\n  {sku}: ERROR {err[:200]}")
            # If we hit a schema field error, the field set is wrong; abort the loop
            if "Cannot query field" in err:
                print(
                    "\n  (Aborting hero SKU loop — schema field invalid; introspection above shows the real fields.)"
                )
                break


if __name__ == "__main__":
    main()
