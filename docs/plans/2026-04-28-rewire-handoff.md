# 2026-04-28 — ShipHero rewire + ATC fix handoff

Major refactor day. Read this before resuming any work on this bot.

## What changed

### Inventory source: Shopify → ShipHero (Merchdrop warehouse 117661)

`quantity_alerts.py` and `weekly_snapshot.py` now read inventory from the
ShipHero Public API. Shopify is no longer trusted because it represents
only ~20% of channel mix; ShipHero is the unified source across Shopify,
TikTok Shop, and Amazon.

`atc_audit.py` is unchanged in source (still hits the Shopify storefront
to detect broken Add-To-Cart) but its scanner regex was fixed (see below).

### New modules

- `src/based_inventory/shiphero.py` — GraphQL client. Bisection pagination
  for `warehouse_products` and `products(has_kits: true)` (the API caps
  each call at 100 and the `data` connection has no after/first cursor;
  we recursively split the date window). Includes
  `fetch_inbound_outstanding_by_sku` (purchase_orders pending, indexed by
  SKU) and `fetch_channel_mix` (orders per shop_name).
- `src/based_inventory/registry.py` — BundleRegistry merging ShipHero
  `kit_components` + `data/set-components.json`. Substring-fallback name
  resolution for marketing-prefixed kit names.
- `src/based_inventory/inventory.py` — `weeks_of_cover` math + per-SKU
  trust filter (`trusted_inventory=False` for any SKU in
  `BundleRegistry.bundle_skus`).
- `src/based_inventory/discontinued.py` — heuristic + manual SKU skip
  filter. Reads `data/discontinued-skus.json` (currently empty; Avi
  populates as needed) plus a heuristic name pattern list (test, BFCM,
  legacy version markers, etc).
- `src/based_inventory/shiphero_auth.py` — refresh access token from
  refresh token via `public-api.shiphero.com/auth/refresh`. Resilient to
  the 28-day access-token expiry.

### State versioning (dedup wipe on deploy)

`AlertState.CURRENT_SCHEMA_VERSION = "v2"`. On load, mismatched
`schema_version` clears `quantity_tiers` and `atc_flags` so prior
title-keyed entries and 5 false-positive ATC flags from the broken-regex
era don't suppress new alerts.

### ATC scanner regex fix

The Playwright crawler in `crawl/atc.py` was missing every PDP because
its anchored regex `/^(ADD TO CART|...)$/i` rejected Based's primary CTA
text `<p>ADD TO CART $28.00</p>` (Instant.so bakes the price into the
leaf). Fixed to allow optional ` $price` suffix(es):
`/^(ADD TO CART|...)(\s+\$[\d.,]+(\s+\$[\d.,]+)?)?$/i`.

Live-verified on Daily Skincare Duo + Volume & Refresh Duo. Regression
tests added in `tests/test_atc_crawler.py`.

### Cron cadence change

`based-inventory-quantity` was every 6h; now daily Mon-Fri 14:00 UTC
(7am Pacific). `based-inventory-weekly` unchanged (Friday 16:00 UTC).
`based-inventory-atc` unchanged (daily 13:00 UTC).

### Slack alert format

Each alert now annotates:
- Tier ladder includes 🚨🚨 OVERSOLD above CRITICAL for `on_hand < 0`.
- `weeks_of_cover` + `velocity_per_day` from ShipHero `inventory_changes`
  (kit-rollup events INCLUDED — they're how kit sales register on
  component velocity).
- Inbound visibility: `📥 X,XXX inbound across N pending POs (latest PO
  YYYY-MM-DD, no ship_date)`. ShipHero's `ship_date` field is null on
  most pending POs so ETAs are usually unavailable; we surface the
  outstanding qty + most-recent po_date instead.
- Bundle cascade: list of bundles whose components include the at-risk
  SKU.
- Footer: 7-day channel mix (e.g. `TTS 70% / Shopify 20% / Amazon 10%`).

## Validation

Carlos confirmed on 2026-04-28: "the numbers are accurate" after
cross-referencing the bot's per-SKU detail against his internal
ShipHero dashboard (DM thread D0AR9378SG4 around 16:00 PT).

Real-world signals captured by the rewired bot:
- Body Wash Guava Nectar: 12 on hand, 0.18w cover at 10/day, bottleneck
  on 9 GN bundles.
- Sea Salt Spray: 252 → 2,893 on hand within 24h after 7 pallets landed
  at the co-packer.
- Curl Cream: hero SKU, ~12K on hand at hero velocity ~5K/day = ~1.5d
  real cover.

## Production state at handoff time

- Local code: `~/tools/based-inventory/` (main branch).
- All three Render crons still in DRY_RUN=1 from before the rewire.
- 60 tests pass; ruff clean.
- `data/discontinued-skus.json` is empty. Carlos confirmed Tallow IS
  coming back; nothing currently belongs in the EOL list.

## Next steps when resuming

1. Push to GitHub. Render's auto-deploy will pick it up.
2. Render env vars: SHIPHERO_REFRESH_TOKEN must be added to all three
   crons (and SHIPHERO_ACCESS_TOKEN as fallback). The `.env.example`
   has the canonical list.
3. Flip `DRY_RUN=0` on quantity + weekly + atc together. Schema-version
   bump will clear stale state automatically on first run.
4. Watch the first Slack post; confirm cascade lists, inbound
   annotations, and channel-mix footer render correctly.

## Known limitations / deferred

- Backorder interpretation: when a SKU has backorder>0 but on_hand is
  healthy, the backorders are usually from bundle orders bottlenecked
  on a different component. Bot doesn't yet distinguish; per-SKU detail
  reports backorder as informational.
- Per-channel VELOCITY (not just order count): would require expensive
  orders+line_items aggregation. Footer shows order-count split which
  is good enough for a Monday-meeting glance.
- Wilshire warehouse 118397: empty (0 active warehouse_products on
  probe 2026-04-28); not queried. Reconfirm if it ever fills.
- ShipHero `ship_date` on POs: null on most pending POs. Inbound
  visibility surfaces outstanding qty without ETA. If David starts
  filling ship_date consistently, the bot will pick it up automatically.
