"""Cross-reference AUDIT_LAYOUT names against the Shopify Admin API to get
each product's canonical 'single component' SKU(s).

Rationale (Avi, 2026-05-09): Shopify is the source-of-truth for which SKU
represents the actual finished good a customer buys. ShipHero contains many
related SKUs per product (packaging components, kits, multi-packs, channel
listings); the substring-fallback in weekly_snapshot can grab the wrong one
when the packaging component happens to have higher on_hand than the real
single (the 2026-05-09 "Body Wash: 25,000" bug — BW-SNTL-PK001 was the
packaging cap, not the finished good).

This probe pulls each AUDIT_LAYOUT name from Shopify, lists every variant
SKU + title, and highlights the 'single component' variants (titled e.g.
"Just One", "Single", or starting with "Single /"). The output is the right
input for editing data/audit-aliases.json.

Run from this repo root:
  shopify store execute --store basedbodyworks.myshopify.com --query "$(python scripts/probe_shopify_canonical_skus.py --print-query)"

Or invoke directly (shells out to the shopify CLI):
  python scripts/probe_shopify_canonical_skus.py
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# Names to cross-reference. Keep in sync with weekly_snapshot.AUDIT_LAYOUT
# for the categories where multiple scent/size variants share one display
# row. Hair Clay, Leave-In, Scalp Scrubber, Wooden Hair Comb are 1:1 with
# their ShipHero SKU and don't need this treatment.
NAMES = ["Body Wash", "Body Lotion", "Deodorant"]

# Variant title patterns that indicate "this is the single-unit finished
# good a customer buys" vs a multi-pack or channel-specific listing.
_SINGLE_PATTERN = re.compile(r"\b(just one|single)\b", re.IGNORECASE)

STORE = "basedbodyworks.myshopify.com"


def build_query(name: str) -> str:
    # Shopify Admin search syntax: per-name wildcard query. We loop over
    # NAMES because the multi-term `OR` form silently misses products in
    # practice (tested 2026-05-09; only the first hit returned).
    return (
        "query ProbeCanonicalSkus { "
        f'products(first: 10, query: "title:*{name}*") {{ '
        "nodes { title status variants(first: 50) { nodes { sku title } } } } }"
    )


def run_shopify_cli(query: str) -> dict:
    """Invoke `shopify store execute` and parse the JSON response. Strips
    the CLI's UI noise (spinner frames, success banner) and isolates the
    JSON payload."""
    proc = subprocess.run(
        ["shopify", "store", "execute", "--store", STORE, "--query", query],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.exit(f"shopify CLI failed (exit {proc.returncode}):\n{proc.stderr[:2000]}")
    # The CLI prepends ANSI spinner frames + a success banner before the
    # JSON. Find the first '{' that opens a balanced object.
    out = proc.stdout
    start = out.find("{\n")
    if start < 0:
        start = out.find("{")
    if start < 0:
        sys.exit(f"Could not find JSON in shopify CLI output:\n{out[:2000]}")
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError as e:
        sys.exit(f"JSON parse failed at offset {e.pos}:\n{out[start:][:2000]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--print-query",
        action="store_true",
        help="Print the GraphQL query and exit (useful for piping to shopify CLI)",
    )
    args = parser.parse_args()

    if args.print_query:
        for n in NAMES:
            print(f"# Query for {n}:")
            print(build_query(n))
            print()
        return

    # Run one query per name, then merge results. Filter to title == name
    # (substring search returns extras like "Body Care Set" for "Body Wash").
    products: list[dict] = []
    for n in NAMES:
        payload = run_shopify_cli(build_query(n))
        for p in (payload.get("products") or {}).get("nodes") or []:
            if (p.get("title") or "").strip().lower() == n.lower():
                products.append(p)
    if not products:
        sys.exit("No products returned. Check the title filter or store auth.")

    print("Audit name → Shopify canonical single-component SKUs")
    print("=" * 78)
    suggested_aliases: dict[str, list[str]] = {}
    for p in products:
        title = p.get("title") or ""
        status = p.get("status") or ""
        variants = (p.get("variants") or {}).get("nodes") or []
        singles = [v for v in variants if _SINGLE_PATTERN.search(v.get("title") or "")]

        print()
        print(f"{title}  [{status}]  ({len(variants)} variants total, {len(singles)} singles)")
        print("-" * 78)
        for v in variants:
            sku = v.get("sku") or "(no SKU)"
            vtitle = v.get("title") or ""
            marker = "  ←  SINGLE" if _SINGLE_PATTERN.search(vtitle) else ""
            print(f"  {sku:<28} {vtitle}{marker}")

        if title in NAMES and singles:
            suggested_aliases[title] = [v["sku"] for v in singles if v.get("sku")]

    print()
    print("=" * 78)
    print("Suggested audit-aliases.json entries (multi-SKU aggregate):")
    print("=" * 78)
    for name, skus in suggested_aliases.items():
        print(f'  "{name}": {{"skus": {json.dumps(skus)}}},')


if __name__ == "__main__":
    main()
