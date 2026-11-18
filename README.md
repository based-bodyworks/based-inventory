# Based Inventory

Bot that reports ShipHero warehouse inventory and customer order velocity, and checks the storefront's Add-to-Cart behavior. Posts to Slack `#alerts-inventory`.

See `docs/specs/2026-04-15-based-inventory-design.md` for the full design.

## Local development

    python3.12 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements-dev.txt
    playwright install chromium
    cp .env.example .env
    # Edit .env with real tokens

    # Run one job locally (dry-run by default via DRY_RUN=1 in .env)
    python -m based_inventory.jobs.quantity_alerts

## Tests

    pytest

## Lint

    ruff check
    ruff format

## Jobs

- `based_inventory.jobs.quantity_alerts` (weekdays, 14:00 UTC)
- `based_inventory.jobs.atc_audit` (Mondays, 13:00 UTC)
- `based_inventory.jobs.weekly_snapshot` (Monday/Wednesday/Friday, 16:00 UTC)
- `scripts/run_daily_velocity.sh` (daily, 11:00 UTC)

Each reads env vars (see `.env.example`) and posts to Slack unless `DRY_RUN=1`.

## Daily SKU velocity

The daily message shows every SKU in the maintained inventory catalog, grouped
by product category. Scents, sizes, minis and retail packaging remain separate.
Zero-sales rows stay visible. Additional sold SKUs are shown with their SKU
codes so a missing catalog entry cannot silently hide demand.

Each row shows the previous complete UTC day's ordered units and the average
units per day over the seven complete UTC days ending on that date. The
seven-day average stays `n/a` until all seven verified daily records are
available. Missing days are named in the message; they never become zero sales.

The source is Merchdrop customer orders from Shopify, TikTok and Amazon
merchant fulfillment. Counts include free items and physical components inside
packs and bundles, counted once, before cancellation or return adjustments.
These are observed orders, not shipments or an estimate of demand during
stockouts. Amazon-held fulfillment, Target store sales and Target replenishment
shipments are outside this warehouse order report.

The workbook remains attached for channel-level detail. Publication checks the
saved checkpoint, CSV and workbook against one another before accepting a day.
Verified daily records use a separate Redis namespace from stock-alert state,
so the daily job does not need to pull seven days of orders every morning.
Same-day source corrections stop for reconciliation. A saved Slack file
reservation is checked before a retry, including an ambiguous upload response.

Stock threshold alerts show the recorded on-hand, available and backordered
stock for a specific warehouse SKU, with its capture time. They do not calculate
sales rates or cover from capped inventory-change samples. Zero available stock
is an allocation warning for that SKU, not proof that every packaging variant
or inventory pool for the product is empty.
