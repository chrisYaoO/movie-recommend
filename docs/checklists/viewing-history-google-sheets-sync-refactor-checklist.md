# Viewing History / Google Sheets Sync Refactor Checklist

Status legend: `[ ]` not started, `[~]` in progress, `[x]` completed, `[?]` blocked or awaiting confirmation.

## Goal

Refactor viewing-history persistence so that PostgreSQL is the system of record and Google Sheets is a synchronized projection. Existing `viewing_history.id` UUIDs must be preserved and written to a hidden `RecordId` Sheet column. The app must support creating, editing, and deleting history records without losing changes when Google Sheets is unavailable.

## Confirmed Decisions

- [x] Preserve existing `viewing_history` rows and UUIDs; do not rebuild or fully re-import history by default.
- [x] Treat `viewing_history.id` as the permanent record identity.
- [x] Treat `source_sheet_name` and `source_row_number` as a mutable Sheet locator, not record identity.
- [x] Keep `source_row_checksum` as content verification data, not identity.
- [x] Add a hidden Google Sheets column named `RecordId` containing the local UUID.
- [x] Make PostgreSQL the system of record after migration.
- [x] Use one-way runtime synchronization: PostgreSQL to Google Sheets.
- [x] Retry failed Sheet writes when the backend starts; do not start a separate Electron child process for synchronization.
- [x] Use soft deletion locally before removing the corresponding Sheet row.
- [x] Do not silently resolve ambiguous migration matches.

## Decisions Still To Confirm

- [x] Direct edits in Google Sheets are unsupported after cutover and may be overwritten by the app.
- [x] `RecordId` is the tenth column after the current A:I fields.
- [x] Changing `watched_date` across calendar years moves the row to the corresponding year-named Sheet tab.
- [x] Deleting the last active viewing makes the movie recommendation-eligible again when an active candidate source exists; watched wishlist state is preserved.
- [x] Deleted history remains soft-deleted indefinitely; no purge is currently scheduled.
- [x] History management uses a dedicated History tab.

## Scope Limits

- [x] No multi-user synchronization.
- [x] No general-purpose bidirectional sync or automatic field-level conflict merging.
- [x] No Kafka, Redis, Celery, or other new queue dependency.
- [x] No full Sheet scan on every normal create, edit, or delete after all rows have `RecordId`.
- [x] No destructive database rebuild.
- [ ] Revisit bidirectional sync only if editing directly in Google Sheets becomes a real requirement.

## Safety Rules

- [x] Do not run any `git` command without explicit permission in the current conversation.
- [x] Run a read-only migration audit before changing PostgreSQL or Google Sheets.
- [x] Export a timestamped migration report before applying changes.
- [x] Do not delete or overwrite an ambiguous Sheet row.
- [x] Validate a Sheet row's `RecordId` before updating or deleting it.
- [x] Never match two viewing events by `douban_subject_id` alone; the same movie may be watched more than once.
- [x] Keep migration snapshots and reports outside `.trash/`; they are audit data, not disposable build output.
- [x] Make every migration write idempotent so an interrupted run can be safely resumed.

## Target Invariants

- [x] Every active local history row has one stable UUID primary key.
- [x] Every synchronized Sheet row has the same UUID in `RecordId` (498/498 verified by post-migration audit).
- [x] A Sheet row number may change without changing record identity.
- [x] Local create, edit, and soft delete commit even when Google Sheets is offline.
- [x] Every committed local mutation creates or replaces one pending sync task in the same database transaction.
- [x] A successful sync removes the pending task; a failed sync retains its attempts and last error.
- [x] Repeated retries produce the same Sheet state and do not append duplicates.
- [x] A newer edit supersedes an older pending upsert for the same history UUID.
- [x] A pending delete supersedes a pending upsert for the same history UUID.
- [x] Recommendation reads ignore soft-deleted history rows.

## Phase 1: Read-Only Inventory And Audit

- [x] Record the current PostgreSQL schema for `viewing_history` and its indexes.
- [x] Record current history counts grouped by `source_sheet_name`.
- [x] Record counts for missing UUIDs, checksums, Sheet names, row numbers, and Douban subject IDs.
- [x] Read all configured history Sheet tabs and capture their headers and row counts.
- [x] Verify that current history data occupies A:I and identify any extra user-managed columns.
- [x] Recalculate the existing A:I checksum for every Sheet row.
- [x] Classify every local/Sheet relationship as:
  - `matched`: current locator and checksum agree.
  - `relinked`: checksum uniquely matches a different Sheet row.
  - `local_only`: local row has no Sheet match.
  - `sheet_only`: Sheet row has no local match.
  - `content_conflict`: locator exists but content differs and no unique checksum match exists.
  - `ambiguous`: one local row has multiple possible Sheet matches.
  - `duplicate_record_id`: one UUID appears in multiple Sheet rows.
- [x] Write a dry-run report with counts and row-level details for every non-`matched` result.
- [x] Review the dry-run report before enabling any writes.

## Phase 2: Database Schema Refactor

- [x] Keep `viewing_history.id` unchanged as the UUID primary key.
- [x] Add nullable `deleted_at TIMESTAMPTZ` to `viewing_history` (applied 2026-07-19; 498 rows preserved).
- [x] Stop using `(source_sheet_name, source_row_number)` as an upsert conflict key.
- [x] Replace the unique source-row index with a non-unique locator index (applied 2026-07-19).
- [x] Ensure normal history reads filter `deleted_at IS NULL`.
- [x] Add a minimal `sheet_sync_outbox` table (applied 2026-07-19):
  - `history_id` primary key and foreign key to `viewing_history.id`.
  - `operation` constrained to `upsert` or `delete`.
  - `attempts`.
  - `last_error`.
  - `updated_at`.
- [x] Add repository operations that update history and upsert its outbox task in one transaction.
- [x] Ensure a later mutation replaces the existing outbox operation for the same `history_id`.
- [x] Apply equivalent SQLite schema/repository changes so current repository tests remain useful.
- [x] Add an idempotent schema migration path for existing installations.

## Phase 3: Google Sheets Adapter

- [x] Extend the Sheets adapter beyond append-only behavior.
- [x] Keep the adapter interface small:
  - Upsert one projection by `RecordId`.
  - Delete one projection by `RecordId`.
  - Read rows for migration/reconciliation.
- [x] Add `RecordId` to newly appended rows.
- [x] Ensure the `RecordId` header exists once and hide its column.
- [x] Exclude `RecordId` from the existing A:I content checksum.
- [x] Locate existing rows by `RecordId`; treat cached Sheet name/row number only as a fast-path hint.
- [x] Validate the hinted row's `RecordId` before modifying it.
- [x] Fall back to searching the `RecordId` column when the cached locator is stale.
- [x] Reject duplicate `RecordId` matches instead of updating multiple rows.
- [x] Update the complete projected row so retries converge on local state.
- [x] If the date-derived Sheet changes, upsert into the target tab before deleting the old row.
- [x] Delete the exact matched Sheet row only after validating `RecordId`.
- [x] Return the final Sheet name, row number, and updated range so the local locator can be refreshed.
- [x] Handle authorization, timeout, quota, missing-tab, and malformed-response errors without losing the outbox task.

## Phase 4: Existing Data Migration

- [x] Implement migration commands with separate `audit` and `apply` modes.
- [x] In `audit` mode, perform no PostgreSQL or Sheet writes.
- [x] Match existing records in this order:
  1. Existing `RecordId`, when present.
  2. Current Sheet name and row number plus matching checksum.
  3. A unique checksum match across history Sheet tabs.
  4. Otherwise classify for review.
- [x] For `matched` rows, write the existing local UUID into `RecordId` (498/498 applied and verified).
- [x] For `relinked` rows, write `RecordId` and update the cached local locator (implemented; current audit has zero).
- [x] For `sheet_only` rows, prepare a proposed local import without applying it until reviewed (31 reviewed legacy exclusions remain unmanaged).
- [x] For `local_only` rows, prepare a proposed Sheet append without applying it until reviewed (implemented; current audit has zero).
- [x] Leave `content_conflict`, `ambiguous`, and `duplicate_record_id` unchanged pending explicit resolution.
- [x] Make migration progress resumable and safe after interruption.
- [x] Re-run `audit` after migration and require zero unexplained mismatches before cutover (498 matched, 31 reviewed exclusions, all other statuses zero).
- [x] Preserve a final migration report containing before/after counts and every manual decision (`data/audits/viewing-history-record-id-migration-final-20260719-004723.json`).

## Phase 5: Create Path Cutover

- [x] Refactor `ViewingHistoryRecordService.record` so Google Sheets is no longer written first.
- [x] Fetch or reuse movie metadata as needed before the local transaction.
- [x] In one local transaction:
  - Insert `viewing_history` with its UUID.
  - Insert or replace its `upsert` outbox task.
  - Apply existing watched-state updates that must be atomic with the history record.
- [x] Return local success even when the immediate Sheet attempt fails.
- [x] Attempt an immediate outbox flush after the local transaction commits.
- [x] Include a concise sync state in the response so the UI can show pending or failed sync without treating the history write as failed.
- [x] Verify that retrying the create request cannot append a duplicate Sheet row.

## Phase 6: Edit And Delete Paths

- [x] Add `PATCH /viewing-history/{history_id}` for editable fields:
  - `watched_date`.
  - `user_rating`.
  - `quality`.
  - `comment`.
- [x] Validate date, rating range, optional text lengths, and record existence.
- [x] Recalculate `source_row_checksum` from the projected A:I values after an edit.
- [x] Commit the edit and `upsert` outbox task atomically.
- [x] Support moving a row between year tabs when the confirmed Sheet-selection rule requires it.
- [x] Add `DELETE /viewing-history/{history_id}` as a local soft delete.
- [x] Commit `deleted_at` and a `delete` outbox task atomically.
- [x] Make repeated delete requests idempotent.
- [x] On Sheet delete success, retain the last validated locator as audit metadata while clearing the pending task.
- [x] Define and implement derived-state repair after deletion:
  - Check whether another active history row exists for the movie.
  - Preserve watched exclusion while another active viewing exists.
  - Apply the confirmed candidate/wishlist eligibility policy only after the last active viewing is deleted.
- [x] Ensure edits and deletes target history UUIDs, never Douban subject IDs or row numbers.

## Phase 7: Sync Worker And App Lifecycle

- [x] Implement one in-process synchronization module behind a small interface such as `sync_pending(limit)`.
- [x] Process pending tasks in deterministic order and cap each batch.
- [x] On success, remove the outbox task and update the cached Sheet locator.
- [x] On failure, increment attempts and retain a concise `last_error`.
- [x] Prevent two worker executions from processing the same task concurrently.
- [x] Trigger a small sync attempt after create, edit, and delete.
- [x] Trigger pending-task synchronization from the FastAPI startup lifecycle.
- [ ] Optionally retry on a modest timer while the backend remains open; do not require it for correctness.
- [x] Do not block the Electron window from opening while retrying old failed tasks.
- [x] Stop the worker cleanly with the existing FastAPI/backend shutdown lifecycle.
- [x] Expose sync health: pending count, failed count, last successful run, and last error.
- [x] Avoid logging credentials, tokens, full comments, or service-account contents.

## Phase 8: History Management UI

- [x] Add a paginated active-history list using UUIDs as row keys.
- [x] Display date, title, rating, quality, comment, and sync state.
- [x] Add edit controls for the confirmed editable fields.
- [x] Add an explicit delete confirmation.
- [x] Update the UI immediately after local success.
- [x] Show `Pending Google Sheets sync` non-blockingly when the outbox task remains.
- [x] Allow retrying a failed sync without resubmitting the history mutation.
- [x] Do not expose Sheet row numbers as user-facing identity.
- [x] Keep soft-deleted records out of the normal history list.

## Phase 9: Tests

### Repository Tests

- [x] Existing UUIDs remain unchanged during schema migration.
- [x] Create/update/delete and outbox mutation are atomic.
- [x] A second mutation replaces the pending operation for the same history ID.
- [x] Soft-deleted rows are excluded from active history and recommendation reads.
- [x] Source Sheet name and row number are no longer identity or upsert keys.

### Migration Tests

- [x] Current locator plus matching checksum becomes `matched`.
- [x] Unique checksum at a new row becomes `relinked`.
- [x] Same movie watched twice is not collapsed.
- [x] Multiple checksum matches become `ambiguous`.
- [x] Duplicate `RecordId` becomes `duplicate_record_id`.
- [x] Audit mode performs no writes.
- [x] Apply mode is idempotent and resumable.

### Sheets Adapter Tests

- [x] Append includes `RecordId`.
- [x] Update finds a moved row by `RecordId`.
- [x] Stale row-number hint cannot update the wrong row.
- [x] Duplicate IDs block mutation.
- [x] Cross-year edit creates the target row before removing the source row.
- [x] Delete validates the UUID before deleting the row.
- [x] Retry after a timeout does not create a duplicate.

### Backend Tests

- [x] Create succeeds locally when Sheets is unavailable and leaves an outbox task.
- [x] Edit succeeds locally when Sheets is unavailable and replaces the pending upsert.
- [x] Delete succeeds locally when Sheets is unavailable and leaves a pending delete.
- [x] Startup processing uses the tested `sync_pending` path and clears recoverable pending tasks.
- [x] Invalid edits return 400; missing IDs return 404 and repeated deletion of an existing soft-deleted UUID is idempotent.
- [x] Recommendation eligibility follows the confirmed last-viewing deletion rule.

### Frontend Tests

- [x] Edit form submits the history UUID and refreshes displayed values (verified against isolated PostgreSQL in the local browser).
- [x] Delete confirmation targets the history UUID.
- [x] Pending/failed sync state is visible without presenting the local operation as failed.
- [x] History remains usable while Google Sheets is unavailable.

## Phase 10: Verification And Rollout

- [x] Run focused repository, migration, Sheets adapter, and API tests; frontend has a build gate but no component-test harness.
- [x] Run the complete backend test suite against an isolated PostgreSQL test instance (`221 passed`, including Sheets 429 retry and legacy blank-header coverage).
- [x] Run the frontend build and desktop tests.
- [x] Run migration `audit` against the real Sheet and database.
- [x] Review all non-matched records before `apply`.
- [x] Apply `RecordId` backfill in resumable batches (initial run stopped at 63 on HTTP 429; resumed safely to 498/498).
- [x] Run the post-migration audit and compare before/after counts (498 local IDs preserved; 498 matched).
- [x] Test one real create, edit, same-year date edit, cross-year date edit, and delete (`b6266dc5-5d8d-40ff-89fc-7ee53c80f808`; final state soft-deleted and absent from Sheet).
- [x] Test offline create/edit/delete followed by a successful worker retry.
- [x] Verify that no duplicate Sheet rows or `RecordId` values were created (498 unique valid UUIDs; duplicate count zero).
- [x] Verify recommendation and wishlist behavior after editing and deleting history (existing active viewing retained; candidate remained inactive; wishlist remained empty).
- [x] Keep the old append-only path disabled but recoverable until the acceptance checks pass.
- [ ] Remove obsolete compatibility code only in a later cleanup after the new path has been stable.

## Documentation Updates

- [x] Update `CONTEXT.md`: PostgreSQL is authoritative; Sheets is a synchronized projection.
- [x] Update `docs/architecture.md` with UUID identity, mutable Sheet locator, outbox, and soft-delete semantics.
- [x] Replace the old `(source_sheet_name, source_row_number)` stable-identity wording.
- [x] Document that runtime sync is one-way and direct Sheet edits are unsupported unless that decision changes.
- [x] Document migration audit/apply commands and rollback/recovery steps.
- [x] Document how to inspect and retry pending sync tasks.

## Acceptance Criteria

- [x] Existing history UUIDs and user data are preserved (498 IDs identical before/after; schema row count unchanged).
- [x] Every reconciled Sheet row contains exactly one valid `RecordId` (498 valid, 498 unique).
- [x] Create, edit, and delete work while Google Sheets is offline.
- [x] Failed remote writes retry after backend startup without duplicate rows.
- [x] Row insertions/deletions in Sheets cannot cause the app to modify the wrong record.
- [x] Ambiguous migration data is reported and remains unchanged until reviewed.
- [x] Editing a date, rating, quality, or comment converges to the same local and Sheet values (real same-year and cross-year audits passed).
- [x] Deleting history removes it from active local behavior and eventually from Sheets (0 Sheet matches, 498 active local rows, outbox empty).
- [x] Full backend, frontend, and desktop verification passes (221 backend tests, frontend production build, 7 desktop tests).

## Deferred Until Proven Necessary

- [ ] Bidirectional synchronization for direct Google Sheets edits.
- [ ] Field-level conflict resolution UI.
- [ ] Dedicated external worker process.
- [ ] Sync history/event log beyond the outstanding outbox task and migration reports.
- [ ] Automatic hard deletion of local soft-deleted rows.
