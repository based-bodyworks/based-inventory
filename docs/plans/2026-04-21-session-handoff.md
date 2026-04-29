# Based Inventory — session handoff, 2026-04-21

Written at the end of an ~8-hour session. Pick up here.

## What this is

Python 3.12 cron bot monitoring Based BodyWorks Shopify inventory + site ATC
state, posting alerts to Slack `#alerts-inventory`. Three cron jobs on Render,
one Key Value (Redis) state store.

- Repo: https://github.com/basedbodyworks-apps/based-inventory
- Local clone: `/Users/avijhingan/tools/based-inventory` (main branch, up to date)
- Render Blueprint: https://dashboard.render.com/blueprint/exs-d7ikrepkh4rs73b2l5o0

## Services (all Render Blueprint-managed)

| Service | URL | Schedule | State |
|---|---|---|---|
| `based-inventory-quantity` | `/cron/crn-d7il51faqgkc73a0lbb0` | `0 */6 * * *` UTC | DRY_RUN=1 |
| `based-inventory-atc` | `/cron/crn-d7il5tm7r5hc73cb940g` | `0 13 * * *` UTC (6am PST) | DRY_RUN=1 |
| `based-inventory-weekly` | `/cron/crn-d7il5tr9aq1c73e2q2mg` | `0 16 * * 5` UTC (Fri 9am PST) | DRY_RUN=1 |
| `based-inventory-state` | Key Value, free plan | n/a | live |

All three crons have `DRY_RUN=1`. The bot logs intended Slack posts to stdout
but does not actually post. Flipping to live is an env-var edit per service.

## Slack integration

- Workspace: **Based Bodyworks** (T06R3B12DR9)
- App ID: A0AUA38CEV7, Bot user: `based_inventory` (U0ATV4E3AQN, B0AUA39HB4Z)
- Channel: **#alerts-inventory** (C0AK6UGA1NJ), **PRIVATE** — bot was manually
  invited via `/invite @Based Inventory`. If the bot ever gets removed,
  re-invite the same way; `chat:write.public` doesn't help for private channels.
- Scopes: `chat:write`, `chat:write.public`
- Bot OAuth Token stored in Render env var `SLACK_BOT_TOKEN` on the
  quantity service; atc + weekly inherit via `fromService`.
- Connectivity test posted earlier this session, confirmed working.

## Shopify integration

- Store: `basedbodyworks.myshopify.com`
- Auth: **Client Credentials Grant** (24h token fetched each run).
  Permanent credentials `SHOPIFY_CLIENT_ID` + `SHOPIFY_CLIENT_SECRET` in Render.
  Original `shpss_` secret is at `~/Desktop/based-shopify-secret.rtf`.
- API version: 2026-01
- Dev Dashboard app: linked to the Based Plus merchant org. A previously-
  orphaned Dev Dashboard org (214174387) was the reason early auth attempts
  got `shop_not_permitted`. Current creds work.
- Scopes: `read_products`, `read_inventory`, `read_locations` (plus a few
  read-only `inventory_shipments*` scopes the user opted into but we don't use)

## What's been shipped today (commits on `main`, newest first)

```
969af91  fix(atc-audit): mark posted only after real Slack post
1d034d3  fix(ci): pin runner to ubuntu-22.04 for Playwright compatibility
3bb3299  fix(ci): package not importable; install chromium in CI
88106f2  feat(atc-audit): require 2 consecutive observations before posting
dda747b  fix(atc-crawler): hydration poll waits for page's own ATC on PDPs
8fcff77  fix(atc-crawler): use Playwright native click for variant picker
5021632  fix(atc-scan): PDP product handle takes precedence over card walk
03aedce  feat(atc-audit): variant-aware PDP iteration
a4fb634  chore(skip-list): exclude 4 TikTok Shop exclusives
ba7b6aa  feat(atc-audit): detect client-side redirects, skip hidden PDPs
fe6a8ea  perf(atc-audit): wait_until='domcontentloaded' — Based pages load slow
3883983  perf(atc-audit): tighten per-URL budget + log each visit
e11a63b  fix(atc-audit): wait_until='load' not 'networkidle'
7dc7614  feat(atc-audit): rewrite for Based theme + multi-ATC per page
1b190be  refactor(alerts): drop all @-mentions from Slack alerts
f85d355  fix(shopify): shrink products page size to stay under query cost cap
2646737  fix(shopify): InventoryLevel.available was removed in API 2026-01
2ce9bbc  fix(render): declare TELEGRAM_* as optional sync:false on atc and weekly
d1cb084  feat(state): support Redis backend via STATE_PATH URL dispatch
ca6dc7f  docs: add Slack app manifest for the Based Inventory bot
3f0db15  feat(auth): fetch tokens via Client Credentials Grant per run
```

## Current behavior validated today

- **Quantity alerts math**: user spot-checked Shampoo, Body Wash, Tallow
  Moisturizer, Hair Elixir against Shopify Admin — all match (modulo
  seconds-of-sales drift). The calculation is correct.
- **Variant-aware ATC**: Body Care Set + Shower Essentials no longer
  false-positive SALES LEAK on scent-combo sets. Native Playwright clicks
  reliably trigger Based's theme variant picker.
- **Client-side redirect detection**: the 4 TikTok Shop products (see
  skip_list.py) redirect `/products/{handle}` to `/pages/not-found` via
  Instant Commerce code-block; crawler detects and skips them.
- **Hydration polling**: crawler waits up to 8s for an ATC belonging to the
  PAGE's own handle (not any ATC on the page). Kills the "banner ATC hydrates
  first, scan runs too early" class of bug.
- **78 tests pass, ruff clean, CI green**.

## What we intentionally did NOT do

- Flip DRY_RUN=0 on any service. User confirmed twice: keep everything
  silent until the ATC persistence dedup has at least one natural
  cross-run cycle.
- Alert on oversold inventory (qty < 0). Currently skipped per Inventory Brain
  behavior. Flagged as open design call.
- Alert on per-scent oversells inside otherwise-healthy products.
  `resolve_single` sums across scents. Tallow Moisturizer (-53), Body Wash
  Caribbean Coconut (-196) and Guava Nectar (-41), and Deodorant Bergamot &
  Vanilla (-208) are currently silent. Also flagged as open design call.

## Immediate next steps

1. **Tomorrow (today, 2026-04-21) 6am PST**: scheduled `based-inventory-atc`
   cron fires naturally against `969af91`. This is the second consecutive
   observation run for any flag that persisted from yesterday. Under the
   2-consecutive-observation rule, flags seen in both runs would become
   "postable" — logs will show `[DRY_RUN] Slack post:` with the payload.
2. **Review Render logs Wednesday morning**: open
   https://dashboard.render.com/cron/crn-d7il5tm7r5hc73cb940g/logs?r=2h
   after the 6am run. Look for `Found N flags` and `[DRY_RUN] Slack post:`.
   Zero spurious products = dedup is working; flip DRY_RUN=0.
3. **If the dedup surfaces real signal**: post those findings to the user,
   confirm the issues are real (not just crawler misses), then flip.
4. **Open design calls the user still owes us answers on**:
   - Should we add a new `⛔ OVERSOLD` alert tier for `qty < 0`?
   - Should quantity alerts break down per-scent for multi-scent products?

## Known quirks — do NOT revert these

- **`pyproject.toml` has `pythonpath = ["src"]`** under pytest config. Without
  this, CI can't import `based_inventory`.
- **CI uses `ubuntu-22.04`**, not `ubuntu-latest`. Playwright 1.44's
  `install chromium --with-deps` references `libasound2`, which Ubuntu 24
  renamed to `libasound2t64`. Upgrade Playwright deliberately before
  bumping the runner.
- **`products(first: 10, ...)` in shopify.py**. Reduced from 50 to stay
  under Shopify's single-query cost cap (1000).
- **`InventoryLevel.available` was removed in API 2026-01**. Use
  `quantities(names: ["available"]) { name quantity }` and resolve by
  name. Don't revert the query to `available`.
- **On PDPs, the page's handle takes precedence over card-walk handle.**
  `_ATC_SCAN_JS` does `pageHandle || productHandleFromCard(el)`. Reversing
  the order reintroduces NO_BUY_BUTTON false positives on every PDP that
  has a cross-sell or recommendation module.
- **`page.get_by_text(label, exact=True).first.click()`**, not
  `page.evaluate('el.click()')`. The JS-level click doesn't reliably fire
  React's synthetic event handlers in Based's theme.
- **`state.mark_atc_flag_posted()` runs AFTER `slack.post_message()` AND only
  when `not cfg.dry_run`**. Marking earlier (or unconditionally) silently
  consumes flags during the dry-run period — they won't post when
  DRY_RUN=0 flips because state already says "posted".
- **Never add to `data/set-components.json` a product you haven't verified
  is a real on-site bundle.** The 4 TikTok Shop products (Texture & Style
  Duo, Shower Duo + Hair Elixir, Deluxe Straight/Wavy Hair Kit, Santal
  Bodycare Essentials) LOOK like sets but their PDPs redirect to
  /pages/not-found. They live in `skip_list.py` instead.

## Quick runbook

### Trigger a manual ATC run
- Navigate to
  https://dashboard.render.com/cron/crn-d7il5tm7r5hc73cb940g/events
  → click "Trigger Run" (top-right). Runtime ~25-28 min.

### Check what a cron WOULD post (under DRY_RUN=1)
- Render logs: `...logs?r=1h`
- Search the text blob for `[DRY_RUN] Slack post:` — the full Block Kit
  payload is dumped immediately after.

### Flip a service to live
1. Open the service's env page (e.g.
   https://dashboard.render.com/cron/crn-d7il51faqgkc73a0lbb0/env)
2. Click **Edit** (top right of Environment Variables panel)
3. Click the eye icon next to DRY_RUN
4. Triple-click the value field, type `0`
5. Click **Save, rebuild, and apply on next run**

### Run the quantity logic locally against live Shopify
See the inline Python in this session's transcript — fetch_access_token +
ShopifyClient.fetch_all_products() + resolve_single + tier matching.
Useful for spot-checking math without touching Slack.

## Files worth knowing

- `src/based_inventory/crawl/atc.py` — Playwright crawler; variant-aware,
  hydration-polling, redirect-detecting. Any regression in ATC false-
  positive rate lives here.
- `src/based_inventory/jobs/atc_audit.py` — Audit orchestration;
  `compute_expected_products`, `_flags_for_observation`,
  2-consecutive-observation gate.
- `src/based_inventory/jobs/quantity_alerts.py` — Tier ladder + bottleneck
  annotation. Silent on negative inventory (design call).
- `src/based_inventory/state.py` — Redis/file state backend with
  `should_post_atc_flag` / `mark_atc_flag_posted` split.
- `src/based_inventory/auth.py` — `fetch_access_token` Client Credentials.
- `src/based_inventory/skip_list.py` — 9 skips including 4 TikTok Shop.
- `data/set-components.json` — 24 sets, all verified real on-site bundles.
- `render.yaml` — Blueprint manifest; managed by Render sync.
- `.github/workflows/ci.yml` — Ubuntu 22.04, chromium install, pytest.
- `docs/specs/2026-04-15-based-inventory-design.md` — original design
  (section 8.1 rewritten this session to document Client Credentials).

## Handoff summary for next session

The bot is functionally complete and validated. One overnight run away
from confirming the persistence dedup works end-to-end. Next actions are
a review (Wednesday AM) and a call on the two design questions. No
blocking technical work left.
