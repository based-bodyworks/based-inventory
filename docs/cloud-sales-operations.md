# Render sales and tracker operations

The active daily SKU report counts native Shopify, TikTok, Amazon and Target sales. Its exact source captures, text, workbook and Slack delivery receipt are saved in the paid PostgreSQL database. Online days follow the report's platform calendar; Target follows the date printed on its retail report.

The cloud migration also maintains warehouse-only demand and stock. Warehouse UTC orders are a separate measure. Amazon FBA sales, TikTok platform fulfillment and Target store sales must not be used as Merchdrop warehouse depletion.

## Deployment status

`render.yaml` defines the active 08:00 Pacific all-channel report. `render.cloud.yaml` is a separate, opt-in definition for warehouse tracking, source refresh and monitoring. Its writers default to `PIPELINE_ALLOW_WRITES=0`, with automatic deployment disabled. Merging code does not activate those writers. Keep the current local tracker writer and monitor until the supervised cloud checks and cutover approvals below are complete.

The proposed sales service now runs `warehouse`, which saves a verified warehouse order extract, archives its workbook in Drive and updates warehouse history, rolling demand and ERP daily warehouse totals. It never posts a warehouse sales report to Slack. The existing all-channel report remains the sole daily Slack report.

## Morning schedule after approved cutover

| Job | Time | Result |
| --- | --- | --- |
| Warehouse tracking | 11:00 UTC | Save yesterday's complete UTC warehouse orders and update warehouse trackers. |
| Active SKU report | 08:00 Pacific | Save and post the native all-channel report. |
| Tracker refresh | 08:20 Pacific | Verify the saved report in Slack; refresh stock, Amazon fulfillment and Target destinations; mirror the exact report into both trackers. |
| Monitor | 09:15 Pacific | Read actual tracker values and Slack bytes again. Fail if required jobs, versions, sources or provider values are missing, stale or different. |

Summer and winter UTC candidates use a Pacific clock guard. A run may start within 60 minutes of its intended local time. The adjacent daylight-saving candidate exits untouched. A delay of an hour or more requires operator attention.

The `operations` command updates three dedicated tabs in both **BASED Dynamic Sales** and **BB SALES VEL / ERP**: `Daily SKU Sales`, `Daily Sales Sources` and `Daily Sales Methods`. It copies the exact cells of the saved Slack workbook, including unavailable values and source qualifications. Each view includes the reporting date, immutable report version and workbook SHA-256 checksum. These are current report snapshots, not append-only history. PostgreSQL retains the historical captures and workbooks.

Existing Verified Sales History, Canonical Sales Raw, Rolling7Demand and Current Inventory keep their warehouse definitions. Canonical Sales Raw and stock-cover columns stay formula-driven. The new native sales tabs do not feed warehouse cover formulas. Historical planning assumptions are preserved.

The Target retailer tabs and cloud XLSX use the parsed Target source already frozen behind the native report. The tracker refresh does not fetch a second Target report. Amazon fulfillment and current stock retain their separate source checks and dates.

## Cloud configuration

Use the same paid, private PostgreSQL database for `DATABASE_URL` on the proposed jobs and `VELOCITY_DATABASE_URL` on the active report. Cron jobs have no persistent disk. Use a direct database connection, keep public database ingress disabled and retain backup/recovery support.

Server credentials are configured directly in Render. Never commit them, paste them into chat or put them in command arguments.

| Setting | Purpose |
| --- | --- |
| `DATABASE_URL` | Private direct connection to the shared PostgreSQL database. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Approved Gmail read, Sheets write and Drive access. Verify access to the existing tracker and report files before enabling writes. |
| `SHIPHERO_REFRESH_TOKEN` | Existing warehouse read credential. |
| `SLACK_BOT_TOKEN` | Approved report bot with file upload and file read access. |
| `SLACK_REPORT_CHANNEL_ID` | Existing approved report channel for the proposed jobs. The active report uses `SLACK_CHANNEL`. Both must name the same channel. |
| `POLAR_API_KEY` | Existing server credential for native online sales and separate fulfillment data. |
| `TARGET_GMAIL_QUERY` | Narrow sender/subject query used by source collection. |
| `TARGET_WORKBOOK_ID` | Existing cloud Target XLSX in Marketing Operations. |
| `PIPELINE_ALLOW_WRITES` | `0` until approved supervised writes and cutover; `1` permits writer commands. |

The cloud Target XLSX must remain in shared drive `0ANDGo4RyWVY2Uk9PVA`, folder `1Fw8-tnitkJKllxKA2ysAgvaGibWFYDxL`. Preserve its file ID, history, formulas and sharing state. Neither owner roadmap nor lifecycle calendar is a write target.

Render cron billing includes a minimum per service plus active runtime, database and storage costs. Keep the approved total within $25/month. Updating existing services in this change does not require creating additional paid services.

## Commands

Run inside the cloud image with secrets supplied by the service environment. Explicit dates make supervised runs repeatable.

```sh
python -m based_inventory.pipeline migrate
python -m based_inventory.pipeline warehouse --day 2026-11-18
python -m based_inventory.pipeline operations --day 2026-11-18
python -m based_inventory.pipeline operations-monitor --day 2026-11-18
```

`warehouse`, `operations` and `migrate` require writes enabled. `operations-monitor` never migrates, collects new sales, creates run records, repairs receipts, posts Slack messages or writes tracker cells. The native report archive is opened with a database-enforced read-only connection for monitoring.

The legacy `sales`, `trackers`, `monitor` and `compare` commands remain available for compatibility. Do not activate the legacy `sales` publisher alongside the current all-channel report. It would post a separate warehouse workbook and its monitor checks the older publication model.

A normal warehouse retry reuses the saved version. `warehouse --fresh --day YYYY-MM-DD` makes a new paid-credit warehouse pull and is only for a confirmed source correction. Review and verify all affected warehouse destinations after a correction. Existing source plans fence older dates from replacing newer stock or fulfillment results.

The native report and its Target source must already have a verified Slack delivery and all seven saved reporting dates before `operations` writes any source cells. A saved Target write plan that differs from the native report source is rejected for reconciliation.

Sheets requests are paced below the default per-user allowance. Bounded retries for HTTP 429 span a full minute so a preceding request burst can clear. The limit and recovery guidance are documented in [Google Sheets usage limits](https://developers.google.com/workspace/sheets/api/limits).

Tracker view publication saves its exact intent before writing. One cross-date lock protects the six dedicated tabs. An older day cannot replace a newer snapshot. A lost provider response is reconciled against the saved values; retries do not blindly overwrite a changed sheet. A renamed, hidden, resized, missing or conflicting owned tab requires investigation. Owner sheets and formula cells cannot be silently reused as new report tabs.

## Supervised checks and cutover

1. Approve and merge the actual PR after independent code review and passing checks. Record the currently active report command and schedule before changing any service settings.
2. Deploy the merged version to the three proposed services with writes disabled. Verify the shared database, Google access, bot channel, Polar access and cloud Target file destination. Run migrations only when required and explicitly enabled.
3. Verify seven complete warehouse source dates and seven native report dates. Keep the two calendars and definitions explicit. Do not substitute zero for missing source days.
4. Pause the local tracker writer before enabling supervised cloud writes. Run `warehouse` for the agreed day, then `operations` after that day's native report is verified. Never manually trigger a service while its current run is active; Render cancels that service's active run.
5. Read back the actual six native report tabs against the frozen workbook, warehouse history and canonical output, rolling demand and stock-cover formulas, Target retailer values and cloud XLSX, Amazon fulfillment data and stock timestamps. Run `operations-monitor`. A saved success receipt alone is insufficient.
6. After explicit cutover approval, enable the new schedules and retire the local tracker writer. Configure Render failure notifications to the approved destination and verify receipt of a test failure alert. Keep the independent local monitor until seven scheduled cloud mornings pass. A manual repair does not count as a scheduled success.

## Rollback and recovery

Disable the new writers first and confirm no run is active. Re-enable the recorded local tracker writer if needed. Preserve the active all-channel report's 08:00 Pacific command and schedule. Do not restore the obsolete warehouse-only Slack publisher. Keep the database, frozen reports, plans and receipts for investigation. The dedicated native view tabs can stay as dated snapshots during rollback; do not delete sales evidence or restore old tracker values over owner corrections.

For database recovery, stop all writers and restore the paid database to a new instance. Verify saved source/workbook hashes and reconcile actual Google and Slack results before switching connections and resuming publication. Database restore does not roll back provider writes.

## Validation limits

Tests cover provider failures, immutable source/report records, interrupted publication, protected cells, date fences and database behavior. CI builds both images. Live credentials, tracker formulas, exact native views, cloud Target readbacks, alert receipt and seven scheduled mornings still require deployment evidence before the migration is called complete.
