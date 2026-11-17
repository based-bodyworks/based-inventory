# Cloud Sales and Tracker Migration

## Run the sales report and trackers without a Mac

Move the daily tracker refresh and its independent monitor to Render. Keep the existing Google spreadsheet IDs and sharing. Generate the merchant report and update merchant tracker values from the same verified, dated sales version. Keep Target retail, Amazon fulfillment and warehouse stock as separate sources with their own collection times.

Render is the approved provider. This document proposes the implementation and cutover details for review. No production deployment or local schedule change is authorized by this file alone. Based pull requests remain open until Avi approves the specific PR after seeing its link.

## Save one verified merchant sales version

Use Render Postgres for normalized daily merchant sales, verification records, workbook bytes, hashes and publication receipts. The daily workbook is small enough to start with database storage; an object store can be added later if measured volume warrants it. Do not depend on cron-container files or Render Workflows' temporary retention as the permanent archive.

Each immutable version records the complete UTC day, warehouse, source order count, excluded-shop count, catalog and mapping hashes, extractor commit, extraction time, normalized SKU/channel rows, report hash and verification outcome. Accept only a fresh schema-7 extraction whose unique order count matches ShipHero before and after complete order and line pagination. A source revision creates a new version instead of overwriting the old version.

All merchant outputs consume the same version ID. Rerunning a publication step retries that version without querying orders again. A database lock prevents competing merchant extractions across daily jobs, repairs and backfills. Use a connection-owned database session lock and checks of the current saved version to prevent older retries from replacing a newer version.

## Publish matching report and tracker values

The daily Render entrypoint extracts and verifies sales, stores the immutable version, updates Verified Sales History, reads back the changed rows, refreshes rolling demand, verifies ERP Daily Sales and Current Inventory, and posts the matching workbook. Record separate receipts for each destination. Google Sheets and Slack cannot be changed in one database transaction; a partial failure stays visibly incomplete and retries only the unfinished destination. Persist the intended Slack publication before sending, reconcile ambiguous responses, and prevent duplicate posts.

Canonical Sales Raw is formula-derived. Never write into its spilled output cells. Verify its recalculated date/SKU/channel values after updating Verified Sales History. Preserve inventory I:P formulas, notes, formats, zero-available behavior, catalog entries and verified history outside the replacement day.

The morning tracker job reads the newest verified merchant version. It never runs a separate merchant order pull. If that version is missing or still being published, it defers merchant updates and reports the gap. A late-data repair must create and publish a new shared version to every merchant destination.

## Move the full tracker refresh to cloud API access

Port the existing validated Target parser, SKU maps, inventory bindings, Amazon normalization and readback checks into version-controlled production code. Replace Codex connector calls and absolute Mac paths with authenticated API clients. Use Google OAuth for Sheets/Drive and read-only Gmail access, plus Polar's supported API for its existing Amazon sales and inventory queries. Preserve UTC scope, provider control totals, truncation checks, kit expansion and separate stock pools.

The Desktop Target Excel workbook becomes a cloud-stored workbook in Marketing Operations, with an optional download for local use. Use shared drive ID `0ANDGo4RyWVY2Uk9PVA` and folder ID `1Fw8-tnitkJKllxKA2ysAgvaGibWFYDxL`; verify both through Drive metadata after upload. Keep its formulas, cached values and product history. Discover whether a suitable existing cloud copy exists before creating a new file. Preserve the existing native tracker file IDs. Read back and validate the saved cloud workbook after upload.

Cloud credentials for Google, Polar and database access have not yet been verified. Reuse only credentials explicitly intended for server use. Do not export Codex session tokens or silently create broader account access. Provision secrets through Render's secret configuration, never Git or logs.

## Monitor expected runs independently

Run a separate Render monitor that checks the expected reporting day even if the pipeline never starts. Compare database version and completion receipts with native sheet readbacks and Slack publication. Distinguish extraction failure, incomplete publication, a still-running job and stale data. Verify the successful-report file hash rather than accepting a zero exit code alone. Keep the cloud monitor running after migration; the temporary local seven-day monitor can retire after its success criteria are met.

Preserve the current daily extraction schedule of 11:00 UTC, the tracker refresh at 07:00 America/Los_Angeles and the monitor at 08:45 America/Los_Angeles. Render cron uses UTC; implement and test Pacific daylight-saving handling explicitly rather than describing a fixed UTC hour as year-round Pacific time.

## Prove parity before changing the active jobs

First run the cloud clients in read-only comparison mode against stored verified fixtures and current native sheets. Exercise duplicate runs, source-count changes, interrupted writes, expired credentials, missing reports, late Target emails, incomplete Amazon coverage, new positive SKUs and daylight-saving transitions. Preserve verified values when a source is incomplete.

After Avi approves merging the actual reviewed PR, merge it and prepare the cloud resources with publishing disabled. Obtain approval for a controlled live test that verifies one complete reporting day, all merchant SKU/channel rows, every rolling product and stock-cover formula, Target product rows, Amazon controls and the uploaded workbook. Only then switch the production entrypoint and disable local writers so there is one active publisher. Keep a read-only comparison monitor for seven scheduled mornings. Do not trigger a Slack-posting live test without approval of that specific test.

Rollback disables new cloud writers, preserves their snapshots and receipts, restores the prior Render command and re-enables the previous local tracker schedule if needed. Never run both writer paths together. Owner roadmap and lifecycle calendars remain read-only throughout.

## Sources

- Avi's request and provider approval: this chat, November 17, 2026.
- Existing source contracts: based-sop-deck skill and references/source-workflow.md, read November 17, 2026.
- Repository baseline: based-bodyworks/based-inventory main, 6fcc9bb20bec1e183c104d3b94e3a8255f6c088c.
- Local tracker and monitor definitions: update-target-daily-sales-tracker and monitor-based-daily-sales-velocity automation.toml, read November 17, 2026. Both remain active locally.
- Render cron limits and UTC schedules: https://render.com/docs/cronjobs
- Render Workflows temporary state and retries: https://render.com/docs/workflows and https://render.com/docs/workflows-limits
- Polar server API authentication: https://support.polaranalytics.com/articles/8432442750-using-mcp-tools-as-an-api
