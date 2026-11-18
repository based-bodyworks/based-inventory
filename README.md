# Based Inventory and Daily Sales

Stock alerts, a daily all-channel SKU sales report, and storefront Add-to-Cart
checks for Based. Reports post to Slack `#alerts-inventory`.

The original inventory and storefront design is in
`docs/specs/2026-04-15-based-inventory-design.md`. The daily sales report is
described below.

## Local development

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
playwright install chromium
cp .env.example .env
```

Configure the required server credentials in `.env` without committing them.
Use `DRY_RUN=1` for stock-alert development. The sales report has a separate
offline `--preview` mode that requires a checked local source capture.

```sh
DRY_RUN=1 python -m based_inventory.jobs.quantity_alerts
pytest
ruff check
ruff format --check
```

## Scheduled jobs

| Job | Schedule in `render.yaml` |
| --- | --- |
| `based_inventory.jobs.quantity_alerts` | Weekdays at 14:00 UTC |
| `based_inventory.jobs.atc_audit` | Mondays at 13:00 UTC |
| `based_inventory.jobs.weekly_snapshot` | Monday, Wednesday and Friday at 16:00 UTC |
| `scripts/post_sales_velocity.py --at-local-time 08:00` | Daily at 08:00 Pacific |

The sales cron uses `0 15,16 * * *` to cover daylight-saving time. The command
accepts startup from 08:00 through 08:59:59 Pacific; the other candidate exits
before reading credentials, sources or the database. A saved delivery receipt
also prevents an ordinary rerun from sending the same report twice.

The sales service uses the existing Python 3.12 `Dockerfile.pipeline` image.
The shared stock-alert and storefront image is unchanged. The separate cloud
source/tracker publishers in `render.cloud.yaml` remain an opt-in proposal with
`PIPELINE_ALLOW_WRITES=0`. Running the daily sales report does not enable them.

## Daily all-channel SKU sales

The Slack message and attached workbook show every physical SKU in
`src/based_inventory/pipeline/cloud_helpers/inventory_catalog.json`, currently
46 rows. Scents, sizes, minis and retail packaging stay separate. Zero-sales
rows remain visible. Each row shows units for the reporting date, a seven-day
daily average, and Shopify, TikTok, Amazon and Target counts.

| Source | Count used |
| --- | --- |
| Shopify, through Polar | Units ordered, including free items; cancelled orders and gift cards excluded; before returns. Recognized marketplace mirror channels are excluded. |
| TikTok Shop, through Polar | Units ordered across seller and TikTok fulfillment, including free items; cancelled orders excluded; before returns. |
| Amazon, through Polar | Units ordered across Amazon and merchant fulfillment; cancelled orders excluded; before returns. |
| Target daily email report | Product-row retail units for the printed report date, including the mapped retail packaging and minis. Replenishment shipments are not store sales. |

The default reporting date is yesterday in `America/Los_Angeles`. Online
sources use the actual Polar workspace calendar and display its timezone.
Target keeps its retailer report date. The combined count joins those source
calendar dates; it does not relabel a UTC warehouse interval as a Pacific day.

Each as-sold kit expands into its component quantities. Separately ordered
loose units are added to those quantities. Additional positive sold SKUs remain
visible with their SKU codes. An unverified product
mapping withholds the affected channel and headline physical-unit totals and
the seven-day average, rather than silently dropping that SKU.

The average is the sum of seven complete reporting dates divided by seven.
Every date must have all four sources, matching online timezones and verified
product mappings. Otherwise the average is `n/a` and the missing dates are
listed. A missing source day never becomes a zero-sales day.

If a verified warehouse order day is already available in the database, the
workbook adds it as a separate Merchdrop reference. It uses its original UTC
date, includes orders before cancellation or return adjustments, and is never
added to native platform sales. The report does not start a warehouse order
backfill to obtain that optional reference.

## Source checks and saved reports

Each online source must reach the end of the requested calendar day, return
the requested filters and date, and match a separate ungrouped provider total.
Truncated inline results require a bounded full CSV download that reconciles
to that control. An explicit provider integrity mismatch blocks the report.
Target must have a complete dated product table with verified product
bindings; daily units and week-to-date units are kept distinct. Any difference
between Target's product-row total and its headline stays visible.

Unavailable, late or failed required sources withhold the all-channel report.
The job records at most one pending-report notice attempt for that date and
channel. A failed send remains a failed run. An uncertain upload is reconciled
against its reserved Slack file before any retry; it does not trigger a second
upload or a fallback duplicate message.

The durable archive stores the actual source evidence and a frozen package
containing the exact message, workbook, source identities and optional
warehouse reference. Reruns use that package. Different same-date sources or
text require reconciliation. The workbook's `Sources` and `Methods` tabs define
the counts, clocks, controls, capture times and remaining limits.

## Persistent history and seven-day bootstrap

Before an approved rollout, set `VELOCITY_DATABASE_URL` on the existing daily
service to the **existing paid PostgreSQL database**. The secret is declared
with `sync: false`; no database is provisioned by this change. Migration
`002_daily_velocity_archive.sql` creates the separate `daily_velocity_archive`
table. Database session locks protect each reporting date, and saved history
and delivery receipts survive process restarts. Redis and ephemeral container
files are not accepted as the production archive.

For an initial seven-day window, import one actual checked `SalesDay.payload()`
JSON capture from `collect_sales_day` per reporting date, including its
underlying provider evidence. Hand-entered totals and layout samples are not
operational source history. With the
database secret configured, the import command saves source history without
posting a report:

```sh
python scripts/post_sales_velocity.py --archive-only \
  --sources /absolute/path/to/checked-sales-day.json
```

Repeat for each of the seven dates. Missing dates keep the average unavailable.
After bootstrap, each normal daily run collects only its reporting date and
reuses checked history; it does not repull seven days every morning.

**Kit history limit:** Each capture stores the recipe definitions used, but
the collector reads current ShipHero kit definitions and the maintained
set-component supplement. It does not retrieve recipes as of each old sale.
If kit contents changed, a historical backfill needs a recipe check before
its component-unit counts can be treated as accurate history. Stockouts can
also limit observed sales; these counts are not an unrestricted demand forecast.

## Offline report preview

Preview requires a checked local source capture and never collects live data,
opens the production database or sends to Slack:

```sh
python scripts/post_sales_velocity.py --preview \
  --sources /absolute/path/to/checked-sales-day.json \
  --out-dir /absolute/path/to/preview
```

An optional `--history /absolute/path/to/local-history` reads previously
checked local captures to show their seven-day window. Remote history URLs
are refused in preview. `--out-dir` saves the exact Markdown message and XLSX
workbook for review.

The older `daily_sales_velocity.py`, `run_daily_velocity.sh` and
`post_velocity_to_slack.py` remain manual warehouse-only tools. They are not
the scheduled all-channel send and their UTC counts must not be added to it.

## Stock alerts

Stock threshold alerts show recorded on-hand, available and backordered stock
for a specific warehouse SKU, with its capture time. They do not calculate
sales rates or cover from capped inventory-change samples. Zero available
stock is an allocation warning for that SKU, not proof that every packaging
variant or inventory pool for the product is empty.
