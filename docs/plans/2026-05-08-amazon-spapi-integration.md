# Amazon SP-API integration for full FBA visibility

**Status:** scoped, not started
**Author:** 2026-05-08
**Driver:** 2026-05-07 ShipHero probe revealed only 2 of 11 hero SKUs have any
ShipHero `fba_inventory` rows, and ShipHero's `FbaInventory` type only exposes
`quantity` (no fulfillable / inbound / reserved / unsellable breakdown).
Curl Cream shows quantity=0 in US marketplace via ShipHero. To get a real
picture of Amazon FBA inventory and depletion, we need to talk to Amazon
SP-API directly.

## What ShipHero gives us today

- `Product.fba_inventory` is a `[FbaInventory]` list with 5 fields:
  `id, legacy_id, quantity, marketplace_id, merchant_id`.
- Most Based hero SKUs return an empty list (meaning they're not listed on
  Amazon FBA, OR ShipHero hasn't ingested them, OR the FBA listing uses a
  different SKU than the Merchdrop physical SKU).
- No depletion events, no inbound, no reserved, no unsellable.
- One marketplace observed so far: `ATVPDKIKX0DER` (Amazon US).
- One merchant_id observed: `A330SBD214O4BU` (Based's Amazon seller account).

This is the wired-in `fba_qty` column in the weekly snapshot and inventory
alerts as of 2026-05-08. It's a best-effort sanity check, not a complete
picture.

## What SP-API gives us

Amazon's Selling Partner API (`/fba/inventory/v1/summaries`) returns rich
inventory state:

| Field | What it tells us |
|---|---|
| `fulfillable` | Units currently available to ship to customers |
| `inboundWorking` | Units in inbound shipments not yet received |
| `inboundShipped` | Units in transit to FBA warehouses |
| `inboundReceiving` | Units being received at FBA warehouses |
| `reservedTotal` | Units allocated to customer orders, FC processing, transfer |
| `researching` | Units in investigation (lost, miscounted) |
| `unsellableTotal` | Units in unsellable status (damaged, defective, expired) |

It also exposes:
- `getInventorySummaries(startDateTime=...)` — delta query for
  inventory-changed-since, enabling efficient daily polling.
- Reports API for full ledger / movement history per ASIN.
- Orders API for actual sales volume per channel / time window.

## Bundle / virtual-bundle behavior on Amazon

Amazon FBA Virtual Bundles (introduced 2020, expanded 2024+) work as follows:
- Brand-owners create bundles of 2-5 ASINs sold from a single product page.
- All ASINs must have FBA inventory and be in 'New' condition.
- When a customer buys the bundle, Amazon picks each component from FBA and
  ships them together.
- Bundle inventory is NOT separately tracked — it's derived from each
  component's FBA inventory. If one component goes OOS, the bundle becomes
  unavailable.
- Component-level FBA depletion is what `getInventorySummaries` reports.

This is structurally identical to how ShipHero's kit-rollup works at Merchdrop:
component depletion is the unit of truth, not bundle depletion. So if/when we
have SP-API access, FBA velocity per component can be computed the same way as
Merchdrop velocity.

US-only feature.

## What setup requires

1. **Amazon Developer Account.** Register at developer.amazon.com.
   Owner-of-record question: Alankar's Amazon Seller Central account is the
   anchor — confirm whether to register the developer app under his login or
   a Based ops login.

2. **Register an SP-API application.** Get LWA credentials:
   - `LWA_APP_ID` (client ID)
   - `LWA_CLIENT_SECRET`

3. **Authorize the app against the seller account.** Carlos / Alankar walks
   through the Amazon Seller Central authorization flow once. Result is a
   long-lived `REFRESH_TOKEN` we store in the bot's environment variables
   alongside the existing ShipHero refresh token.

4. **AWS IAM role (deprecated requirement).** As of 2024, Amazon dropped the
   AWS IAM Sigv4 requirement; SP-API now accepts plain LWA-issued bearer
   tokens. So no AWS account needed, just LWA. Verify this is still the case
   at integration time — Amazon has changed their auth model multiple times.

5. **Bot integration.** Add a new `based_inventory.amazon` module:
   - `AmazonClient` with auth + token refresh
   - `fetch_fba_inventory_summaries()` — list ASINs with full breakdown
   - `fetch_fba_inventory_changes(start_dt)` — delta since timestamp
   - Wire into `quantity_alerts` (FBA quantity column, like ShipHero
     fba_inventory but with the rich breakdown) and `weekly_snapshot`
     (replace the ShipHero-sourced `fba_qty` with the SP-API one).
   - Optionally: a new `fba_anomaly_alerts` job that flags unsellable spikes
     (returns / damages bursts) which are currently invisible.

6. **SKU mapping resolution.** Amazon ASINs ↔ Merchdrop SKUs is not
   guaranteed to be a 1:1 mapping. Some Based ASINs are bundle listings or
   FBA-specific multi-pack SKUs that don't match Merchdrop SKUs. We'll need
   either:
   - Manual mapping file (similar to `set-components.json` for sets) OR
   - A heuristic SKU-to-ASIN match at runtime (substring / prefix patterns)
   - Probably easier to start with a `data/amazon-sku-mapping.json` file
     that lists each ASIN with its canonical Merchdrop SKU + bundle expansion
     (if it's an Amazon multi-pack).

## Recommended sequencing

1. Confirm SP-API setup ownership with Carlos / Alankar (15 min conversation).
2. Build a one-off probe like `scripts/probe_fba_inventory.py` but hitting
   SP-API directly to enumerate every ASIN + inventory state, dump to JSON.
   This validates auth + reveals the real ASIN list before any code wiring.
3. Build the SKU-mapping file (manually) using the probe output.
4. Wire SP-API client into the bot.
5. Replace the ShipHero `fba_inventory.quantity` reads in
   `quantity_alerts.py` and `weekly_snapshot.py` with SP-API data. Keep the
   ShipHero version as a fallback for the marketplace_id surfaced data.
6. Add `fba_anomaly_alerts` for unsellable / damage / inbound-stuck signals.

## Effort estimate

- Probe + auth setup: 1 day
- Bot integration: 2-3 days
- SKU mapping file population: depends on Based's ASIN catalog size; probably
  1 day of stare-at-spreadsheet work for Carlos or me.

## What this unlocks

- Full Amazon FBA inventory visibility (currently invisible for ~9 of 11 hero
  SKUs per the 2026-05-07 ShipHero probe).
- Amazon FBA velocity per SKU, which is the unaccounted-for slice of Carlos's
  All-Channel Velocity sheet (~25% of unit volume per his Q1 baseline).
- Anomaly detection for unsellable / damaged inventory at Amazon, which is
  separate from the Merchdrop adjustments the new `anomaly_alerts` job
  catches.
- Closer reconciliation between bot-reported velocity and Carlos's xlsx,
  removing one of the two systematic gaps (Merchdrop-only scope; the other
  being the bot's burst-rate-vs-calendar-avg framing, addressed separately).
