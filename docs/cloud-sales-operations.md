# Render sales and tracker operations

This change moves Based's daily sales publishing, tracker refresh and morning checks to Render. The report and merchant tracker updates use one saved, verified sales version. A separate check reads the actual report and tracker values again.

## Current release status

This is deployment preparation. The existing Render sales job and Mac automations remain active. The new Render definition is `render.cloud.yaml`, separate from the active `render.yaml`. Importing it creates paid resources, so review its resource preview before importing. New writers default to disabled. Merging this change does not switch the current jobs.

The release still requires the actual pull request to be approved and merged, cloud credentials and a cloud Target workbook to be configured, a supervised live publication to pass, and the old writers to be disabled at cutover. Do not run both merchant publishers together. Their databases and locks do not coordinate with the old shell job.

## Daily schedule

| Job | Time | Result |
| --- | --- | --- |
| Merchant sales | 11:00 UTC daily | Save yesterday's complete UTC warehouse orders, archive the exact workbook, update merchant trackers, then publish the matching workbook to Slack. |
| Trackers | 07:00 America/Los_Angeles daily | Use that saved merchant version, refresh Target retail, Amazon-held fulfillment and warehouse stock, and update the cloud Target workbook. |
| Check | 08:45 America/Los_Angeles daily | Read saved job records and actual report/tracker values. Exit with a failure if results are missing, stale or different. |

Render schedules use UTC. The tracker and check jobs each have summer and winter candidates. A Pacific clock guard allows a start within 60 minutes of the scheduled time and excludes the adjacent daylight saving candidate. Delays of an hour or more need operator attention. The check can run while a writer is active and treats an interrupted or overlong run as a problem. Configure Render's failure notifications to an approved destination and verify receipt of a test notification before disabling the local monitor. This release does not send separate monitor messages to Slack. A Render-wide outage can also stop its monitor; retain an independent heartbeat check if outage detection is required.

## Cloud setup

Use the same Render region for all three jobs and a paid PostgreSQL database. The database stores report bytes, daily normalized sales, write plans and readback results. Cron jobs have no persistent disk. A direct database connection is required because the shared writer lock belongs to the connection. Do not use a transaction-pooling URL. Keep public database access disabled and enable the paid database's supported backup and recovery features.

The Render Blueprint is a proposal, with automatic deployment disabled and `PIPELINE_ALLOW_WRITES=0`. Choose paid database and compute sizes in Render's resource preview; no monthly total is promised here. Render bills active cron time with a $1 monthly minimum for each cron service, plus database and storage charges. See [Render cron billing](https://render.com/docs/cronjobs) and [Render pricing](https://render.com/pricing).

Configure these secrets directly in Render. Do not copy credentials from a Codex session or commit them to Git.

| Setting | Purpose and required access |
| --- | --- |
| `DATABASE_URL` | Private direct PostgreSQL URL for the shared database. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | Dedicated OAuth grant. Gmail read-only for Target messages, Sheets write access to the two permitted trackers, and Drive access to the report/workbook files in Marketing Operations. Use the least access that can reach those existing shared files. Verify actual file access before enabling writes. |
| `SHIPHERO_REFRESH_TOKEN` | Server credential for the existing read-only warehouse stock and order queries. |
| `SLACK_BOT_TOKEN` | Existing approved report bot, with file upload and file read access for content verification. |
| `SLACK_REPORT_CHANNEL_ID` | Approved inventory report channel. Verify the bot can read and share files there. |
| `POLAR_API_KEY` | Supported Polar tool API credential with Amazon sales and fulfillment dimensions available. |
| `TARGET_GMAIL_QUERY` | Narrow sender/subject query for Target's daily retail report. The collector also checks the report date and content. |
| `TARGET_WORKBOOK_ID` | Drive ID of the cloud Target XLSX, preserving the existing Daily Data, Summary, charts and historical values. |
| `PIPELINE_ALLOW_WRITES` | `0` until approved publication. `1` permits the explicit migration and publishing commands. |

The cloud Target workbook must be in shared drive `0ANDGo4RyWVY2Uk9PVA`, folder `1Fw8-tnitkJKllxKA2ysAgvaGibWFYDxL` (Marketing Operations). Upload the original workbook once, preserve its history and formulas, and verify Drive metadata for both IDs before setting the environment variable. The Mac workbook is no longer the cloud destination. Set the source query and workbook ID only after read-only access succeeds.

## Commands

Run inside the cloud image with secrets supplied through the service environment, never on the command line. Explicit dates make recovery repeatable.

```sh
python -m based_inventory.pipeline migrate
python -m based_inventory.pipeline compare --day 2026-11-16
python -m based_inventory.pipeline sales --day 2026-11-16
python -m based_inventory.pipeline trackers --day 2026-11-16
python -m based_inventory.pipeline monitor --day 2026-11-16
```

`migrate`, `sales` and `trackers` require writes enabled. `compare` and `monitor` do not migrate, create run records, extract merchant orders, or write to providers. They require an existing saved version and read the live destinations against saved expectations. Before the first publication there are no saved destination expectations, so an incomplete comparison is expected and does not prove parity. Check source credentials read-only before bootstrap. Preparing a durable, operator-accessible evidence record for all seven reporting dates is still required before the supervised test; this PR does not include the private recovery artifacts. For a November 16, 2026 test, verify November 10 through November 16 inclusive. For a different test day, use that day and the six preceding UTC dates. Record the evidence location, source coverage and per-day comparisons in the deployment record, then verify the tracker history against it. Never mark missing source days as zero to bypass coverage.

A normal `sales` retry reuses the saved version. `--version` selects an existing version and rejects an older version superseded for that date. `sales --fresh --day YYYY-MM-DD` performs a new paid-credit warehouse pull and saves a new version. Use it only for a confirmed source revision. Re-run the matching tracker job and verify the new report and all tracker destinations afterward. Source refreshes for an older date are blocked once a later reporting day has saved a source plan, so an old retry cannot replace current stock or Amazon coverage. Correct historical Target or Amazon data through a separate reviewed repair. Repairing an older date preserves the latest rolling demand window. After a historical correction, also create and publish a fresh version for the latest reporting day so its saved demand expectations include that correction.

A session lock prevents the new sales and tracker jobs from writing concurrently. If a job is busy, wait for its outcome before manually triggering another job. Render cancels an active run when that same cron service is manually triggered. A failed or interrupted job remains visible in monitoring; a saved success alone cannot make live readbacks pass.

## Verification and retry rules

Merchant verification requires stable warehouse order counts and complete order and item pagination. Saved CSV totals, workbook rows and normalized data must match. Bundle components are already included. Merchant ordered units exclude Target replenishment and Amazon-held fulfillment; Target retail and Amazon-held units use separate collectors and controls.

The merchant publisher updates Verified Sales History, rolling demand and ERP Daily Sales. Canonical Sales Raw is read-only formula output. Current Inventory I:P stays formula-driven. The publisher rejects an unexpected formula or validation in a value-write destination. Source adapters update only the approved stock and separate sales ranges and verify the native results.

Google writes have bounded retries and native readback. Report archives reserve a Drive file ID before upload. Slack reserves a file ID before upload and records it durably. If a Slack upload has an uncertain outcome, the retry verifies that same file rather than allocating another. A crash before upload completion can require an operator to reconcile or complete the reserved file. Do not delete the pending receipt or automatically reupload to make a failed check disappear.

Notifications are handled by Render's job failure alerts in this release. The complete report message template is defined in `slack_report.py`; review its wording and approved channel before the supervised publication. No test message has been sent by preparing this change.

## Cutover and rollback

1. Approve and merge the actual PR after code review and passing checks.
2. Review Render's resource and cost preview, import the separate Blueprint, set credentials, initialize the database, and verify read access. Leave publishing disabled outside the supervised test.
3. Prepare the seven-day evidence record described above and confirm the tracker history against it. Record the old Render command and schedule, and the two local automation definitions, in private deployment evidence.
4. Pause the old merchant publisher for the supervised test. Run the new sales and tracker commands for one agreed reporting day. Read back the Slack and Drive file hashes, merchant values, separate Target and Amazon data, warehouse formulas and the cloud Target workbook. Confirm the same version is recorded for both jobs.
5. After explicit cutover approval, enable the new scheduled writers and disable the old merchant publisher and local tracker writer. Keep the local monitor until seven scheduled cloud mornings pass. Confirm the Render failure-notification destination before retiring it.
6. For rollback, disable the new writers first, confirm no run is active, restore the recorded old Render command and schedule, and re-enable the old tracker writer. Restore an earlier tracker value only from a verified source, preserving correction notes. Keep the database, report files and receipts for investigation.

For database recovery, stop all new writers, restore the paid database to a new instance at the chosen recovery time using Render's restore workflow, verify saved version/report hashes and readback receipts, then switch the private connection URL. A restored database can lag provider writes. Reconcile native files and cells before any resumed publication; do not assume database restore rolls back Google or Slack.

## Validation limits

The repository tests use synthetic source fixtures, provider failures and an actual local PostgreSQL instance. CI also builds both Docker images. Cloud credentials, resource provisioning, provider parity, alert delivery and scheduled operation must be proven during deployment. A passing test suite does not prove those live steps are complete.
