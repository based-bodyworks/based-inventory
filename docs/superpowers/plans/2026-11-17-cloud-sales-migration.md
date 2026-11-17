# Cloud Sales and Tracker Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the complete tracker refresh and independent monitor on Render, with report and merchant trackers consuming the same verified sales version.

**Architecture:** Keep the existing merchant extractor and validation rules. Persist immutable daily versions and delivery receipts in Render Postgres, then publish all merchant outputs from one version. Port Target, Amazon and stock collectors to server API clients; run an independent cloud monitor and preserve local jobs until an approved cutover passes parity checks.

**Tech Stack:** Python 3.12, requests, openpyxl, PostgreSQL with psycopg, Render Docker cron services, Google Sheets/Drive/Gmail APIs and Polar API. Amazon Fulfillment Network (AFN) is the separate Amazon-held fulfillment source. Retain tested JavaScript data-engine logic where rewriting would change behavior.

**Spec:** ../../2026-11-17_Cloud Sales and Tracker Migration Brief.md

## Global Constraints

- Keep Google spreadsheet IDs 1vI9dM06MtptojGSxBAfNqOu0l0ZPyAGj0-D88ZF82eY and 1TE-gFZLELr1BYfaTkhaAqhSkvuid7a8X0cZthyA33I4 and their sharing unchanged.
- Accept only fresh schema-7 merchant extracts with full order/line coverage and stable source counts. No schema-6 reuse or independent tracker merchant pull.
- Never write into Canonical Sales Raw spill cells or replace Current Inventory I:P formulas with constants.
- Keep merchant, Target retail, Amazon fulfillment and warehouse inventory scopes distinct. Preserve the two protected owner calendars as read-only.
- No production schedule or secret changes, manual Slack-posting test, merge or cutover until its required explicit approval.
- No credentials in source, logs, snapshots or fixtures. API errors must be redacted.

## Review Focus

- A sheet write succeeds but the response is lost: retry and readback must not duplicate rows or roll back a newer version.
- A Slack upload succeeds but the response is lost: reconcile stored publication identity before retrying.
- A previous UTC day gains source orders during a pull: reject the partial version and retry without publishing.
- A Target report, Amazon date or positive SKU is missing: preserve verified values and report the exact incomplete scope.
- A job never starts or Pacific daylight saving changes: the independent monitor still identifies the expected reporting day.

---

### Task 1: Persist verified daily sales and resumable publication state

**Files:** Create src/based_inventory/pipeline/{models,store}.py, migrations/001_sales_pipeline.sql, tests/test_pipeline_store.py. Modify requirements.txt and Dockerfile only for required runtime support.

**Interfaces:** `VerifiedDay.from_extract(day, checkpoint, csv_rows, report_bytes, provenance) -> VerifiedDay`; `Store.save_version(verified_day) -> str`; `Store.lock_merchant_pull() -> ContextManager`; `Store.record_receipt(version_id, destination, digest, provider_id) -> None`.

- [ ] Write failing tests for schema mismatch, incomplete/current day, mismatched SKU/channel totals, duplicate keys, altered report hash and missing stable-count evidence.
- [ ] Run those tests and verify they fail for the missing implementation.
- [ ] Implement immutable version records, report bytes, deterministic digests and PostgreSQL migrations. Restrict stored order evidence to counts and safe identifiers rather than customer data.
- [ ] Test actual PostgreSQL concurrency: two writers cannot extract concurrently; stale versions cannot replace newer receipts; identical retries reuse the same version.
- [ ] Run focused tests and commit the tested persistence layer.

### Task 2: Publish merchant trackers and Slack from the saved version

**Files:** Create src/based_inventory/pipeline/{google_api,merchant_publish,runner}.py, scripts/run_sales_pipeline.py, tests/test_merchant_publish.py and tests/test_sales_pipeline.py. Reuse scripts/daily_sales_velocity.py and scripts/post_velocity_to_slack.py behind explicit adapters.

**Interfaces:** `extract_day(day, out_dir) -> VerifiedDay`; `publish_merchant(store, version_id, sheets_client) -> DeliveryResult`; `publish_report(store, version_id, slack_client) -> DeliveryResult`; `run_day(day, mode) -> RunResult`. Modes must include read-only comparison and publish.

- [ ] Write failing tests for a complete saved day, a 14,197-to-14,198 source revision, a lost Sheets response, a lost Slack response, 429/5xx errors, an expired OAuth token, a newer day version racing an old retry, and negative/zero stock cover.
- [ ] Implement bounded API retries and token refresh without raw response or secret logging. Derive approved sheet IDs and metadata before planning row-scoped writes.
- [ ] Update Verified Sales History from saved normalized rows; preserve provenance and corrections. Verify Canonical Sales Raw by keys after recalculation without writing its output cells.
- [ ] Derive seven-day demand from durable verified history plus the same version, preserve the product map and refresh ERP Daily Sales. Verify all merchant inventory formulas and cover.
- [ ] Persist per-destination readback receipts, publish the matching workbook once, and mark success only after required receipts agree on version and digest.
- [ ] Run source fixtures, meaningful failure-path tests, existing tests and lint; commit.

### Task 3: Port Target, Amazon and stock refresh to server APIs

**Files:** Create src/based_inventory/pipeline/{target_refresh,amazon_refresh,inventory_refresh,workbook_archive}.py and tests/test_cloud_tracker_refresh.py. Port the relevant pure helpers, maps, bindings, annotations and fixtures from the based-sop-deck skill into a versioned production package; retain their tests and provenance.

**Interfaces:** `refresh_target(day, gmail, sheets, drive) -> SourceReceipt`; `refresh_amazon(day, polar, sheets) -> SourceReceipt`; `refresh_inventory(snapshot_version, shiphero, sheets) -> SourceReceipt`; `refresh_trackers(day, store, clients, mode) -> RunResult`.

- [ ] Write failing parity tests from independently parsed Target emails, normalized AFN sales with provider controls, actual retail stock bindings and the existing workbook. Include Sunday boundaries, late emails, unknown positive products, duplicate messages, truncated feeds, zero-available rows and wrong AFN timezone.
- [ ] Implement read-only Gmail collection, supported Polar tool API calls, and exact existing scope/control validation. Verify API credentials with minimal read-only requests before enabling any cloud writer.
- [ ] Use the stored merchant version, never another merchant pull. Preserve separately timed stock, Target retail and Amazon-held inventory.
- [ ] Discover or create the cloud Target workbook in Marketing Operations, preserve formulas/history, verify Drive destination and uploaded content, and avoid a required Mac file path.
- [ ] Compare every permitted write to its independently derived source and native readback. Require all required source receipts for a complete tracker refresh.
- [ ] Run old helper regressions and new adapter tests, then commit.

### Task 4: Add independent Render monitoring and safe deployment configuration

**Files:** Create src/based_inventory/pipeline/monitor.py, scripts/monitor_sales_pipeline.py, tests/test_pipeline_monitor.py and docs/cloud-sales-operations.md. Modify render.yaml with opt-in cloud tracker/monitor definitions and necessary database bindings.

**Interfaces:** `expected_reporting_day(now_utc, timezone) -> date`; `check_pipeline(day, store, provider_readbacks) -> HealthReport`; `notify_once(health_report, state, slack_client) -> None`.

- [ ] Write failing tests for no run record, active extraction, failed extraction, missing report, a stale sheet version, mismatched hashes, partial Target/AFN coverage, duplicate alerts and Pacific daylight-saving boundaries.
- [ ] Implement a monitor independent of the sales task, with expected-day/deadline rules and stable alert identities. Include complete proposed bot alert templates for approval before activation.
- [ ] Configure one active merchant writer, bounded pending-refresh behavior and safe cron UTC/Pacific conversion. Keep new writers disabled until cutover approval.
- [ ] Document exact secret names, access scopes, database restore procedure, read-only comparison commands, report retry procedure and rollback commands without secret values.
- [ ] Run Docker build, tests and lint; commit.

### Task 5: Review, prove parity and perform an approved cutover

**Files:** Add dated private evidence and update docs/cloud-sales-operations.md with tested procedures; do not store sensitive runtime data in Git.

- [ ] Run the full repository checks and a clean PostgreSQL migration/concurrency suite, with no live Slack posts or tracker writes in comparison mode.
- [ ] Obtain a fresh whole-branch code review against the exact final head, covering all side effects, failed/ambiguous writes, state, scopes and protected files. Fix findings and repeat checks after changes.
- [ ] Open a PR into main with scope, checks, known risks, rollout and rollback. Add codex and codex-automation labels if available. Show Avi the actual link; leave it open for review and explicit merge approval.
- [ ] After approved merge and secret setup, request the specific live comparison/publication test and verify every destination from the saved dataset, not from its own write payload.
- [ ] After approved cutover, disable local writers and the old standalone report publisher only when the new cloud path is confirmed active. Preserve the previous commands and schedule records for rollback.
- [ ] Verify seven consecutive scheduled cloud mornings using run records, reports, source coverage and tracker readbacks. Retire the temporary local monitor only after those checks pass; retain the permanent cloud monitor.
