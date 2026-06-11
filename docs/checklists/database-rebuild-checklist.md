# Database Rebuild Checklist

This is a historical execution and verification record for the database rebuild. Test counts and measurements inside completed sections describe the run at that time; use `README.md` for current commands and current expected test totals.

Status legend: `[ ]` not started, `[~]` in progress, `[x]` completed, `[?]` blocked or awaiting user confirmation.

## Ground Rules

- [x] Record that no `git` command may be run without explicit user permission in the current conversation.
- [x] Do not touch frontend files during this database rebuild.
- [x] Ask before making any destructive database change.
- [ ] Ask before resolving ambiguous schema, import, or scraper behavior.
- [ ] After each step, report what changed and wait for user confirmation before continuing.

## Design Decisions To Confirm

- [x] Confirm the new `viewing_history` source identity: `(source_sheet_name, source_row_number)`.
- [x] Decide whether to keep a non-unique row hash only as a change-detection checksum.
- [x] Confirm exact table/column naming for the new key: `source_sheet_name` and `source_row_number`.
- [x] Confirm whether old `source_file` should be retained only as a compatibility/read-model field: no, only old progress JSON compatibility readers should parse it.
- [x] Confirm destructive rebuild scope: clear all tables except `candidate_subject_queue` and `candidate_pool`.
- [x] Confirm `viewing_history` should not depend on `movies.id`; it should store `douban_subject_id` directly, with `movie_id` only as an optional backfilled cache.
- [x] Confirm batch rebuild mode: import all Google Sheets viewing history first, then fetch details into `movies` from distinct subject IDs in `viewing_history`.
- [x] Confirm incremental update mode: append/update one current viewing-history row; if the subject already exists in `movies`, use it directly, otherwise search/fetch/enqueue through the detail path.
- [x] Confirm progress JSON subject-id selection priority: `manual_id_persisted` > `review_confirmed_persisted` > `auto_matched_persisted`.
- [x] Confirm same-priority confirmed subject-id conflicts should be reported and skipped instead of guessed.

## Rebuild Plan

- [x] Inspect code references for the current PostgreSQL schema and source-row identity.
- [x] Inspect current PostgreSQL row counts before any destructive rebuild.
- [x] Draft the schema migration plan for `viewing_history` unique identity.
- [x] Clear/rebuild all non-candidate tables only after user approval.
- [x] Ignore local `.xlsx` imports for the rebuild path.
- [x] Prepare Google Sheets direct import path for the rebuild.
- [x] Use `data/cache/import-auto-match-progress.json` to map sheet name / `source_row_number` to known Douban subject IDs.
- [x] Populate `viewing_history` from Google Sheets rows plus progress JSON matches without fetching Douban details after user approval.
- [x] Clear the `movies` table only after user approval.
- [x] Remove `display_title` and `original_title` from the `movies` schema.
- [x] Add a safe rebuild database job with inspect, dry-run, and explicit destructive confirmation.
- [?] Rebuild `movies` by fetching Douban detail pages for distinct subject IDs referenced by `viewing_history` after user approval.
- [?] Backfill `viewing_history.movie_id` after corresponding `movies` rows exist after user approval.
- [?] While fetching watched-movie detail pages, enqueue Douban recommendations into `candidate_subject_queue` after user approval.
- [ ] After viewing-history rebuild completes, run Douban Top250 discovery.
- [ ] While processing Top250 detail pages, enqueue Douban recommendations into `candidate_subject_queue`.
- [ ] Verify final row counts for `viewing_history`, `movies`, `candidate_subject_queue`, and `candidate_pool`.
- [x] Update README / architecture docs with the new rebuild workflow and commands.

## Completed In Current Refactor Step

- [x] Rename formal viewing-history source identity fields to `source_sheet_name` and `source_row_number`.
- [x] Rename row hash semantics to `source_row_checksum`.
- [x] Make repository uniqueness depend on `(source_sheet_name, source_row_number)` instead of checksum.
- [x] Keep old `source_file` / `source_row_hash` parsing only for legacy progress JSON compatibility.
- [x] Update Google Sheets sync and record-watched service to use sheet name directly.
- [x] Update backend tests for the new field names and identity contract.
- [x] Remove `display_title` and `original_title` from movie persistence schema and repository SQL.
- [x] Execute the confirmed database clear step.
- [x] Add Google Sheets confirmed-progress replay mode for rebuilding `viewing_history` after a cleared database.

## Current Refactor Impact Notes

- `backend/app/db/postgres_repository.py` and `backend/app/db/sqlite_repository.py` no longer create or persist `movies.display_title` and `movies.original_title`.
- `viewing_history` now uses `source_sheet_name` and `source_row_number` as the unique source identity in repository code.
- Domain DTOs now carry `source_sheet_name` and `source_row_checksum`.
- Google Sheets sync no longer depends on the xlsx alias for normal sheet identity.
- The existing progress JSON can still be joined by extracting the sheet name from old `source_file` values like `MOVIES.xlsx#2026`, plus `source_row_number`.
- `display_title` and `original_title` still appear in `DoubanMovieDetail` parsing and manual-review display, but repository persistence no longer stores them in `movies`.
- `jobs/rebuild_database.py` provides `inspect` and `clear-non-queue-tables`; the clear command defaults to dry-run unless `--confirm-clear-non-queue-tables` is supplied.
- `jobs/sync_google_sheets_history.py --replay-confirmed-progress` must rebuild `viewing_history` from Google Sheets rows plus confirmed progress JSON subject IDs without treating progress JSON as already-persisted resume state.
- Corrected design: Google Sheets replay must not fetch Douban detail pages; recommendation enqueueing belongs to the later detail-loading step.

## Current PostgreSQL State Before Destructive Rebuild

- [x] Read-only inspection completed.
- `movies`: 464 rows.
- `viewing_history`: 467 rows.
- `candidate_subject_queue`: 254 rows.
- `candidate_pool`: 1 row.
- `history_recommendation_discovery`: 0 rows.
- `recommendation_sessions`: 0 rows.
- `recommendation_items`: 0 rows.
- `feedback`: 0 rows.
- `wishlist`: 0 rows.
- Blocking detail: `candidate_pool.movie_id` has a foreign key to `movies.id`; the only current candidate pool row points to Douban subject `1292052` / `肖申克的救赎 The Shawshank Redemption`.
- [x] Decision confirmed: preserve `candidate_subject_queue`, convert current `candidate_pool` rows back into queue rows, then clear `candidate_pool` with the other rebuild tables.
- Dry-run command already verified:
  - `.\.venv\Scripts\python.exe -m jobs.rebuild_database inspect`
  - `.\.venv\Scripts\python.exe -m jobs.rebuild_database clear-non-queue-tables --dry-run`
- Confirmed destructive command executed:
  - `.\.venv\Scripts\python.exe -m jobs.rebuild_database clear-non-queue-tables --confirm-clear-non-queue-tables`

## Current PostgreSQL State After Confirmed Clear

- [x] The previous `candidate_pool` row for Douban subject `1292052` was preserved back into `candidate_subject_queue`.
- [x] Actual Postgres columns `movies.display_title` and `movies.original_title` were dropped.
- [x] Independent post-clear inspection completed.
- `movies`: 0 rows.
- `viewing_history`: 0 rows.
- `candidate_subject_queue`: 254 rows.
- `candidate_pool`: 0 rows.
- `history_recommendation_discovery`: 0 rows.
- `recommendation_sessions`: 0 rows.
- `recommendation_items`: 0 rows.
- `feedback`: 0 rows.
- `wishlist`: 0 rows.
- Old movie columns still to drop: none.

## Google Sheets Confirmed-Progress Replay Preparation

- [x] Corrected rebuild mode:
  - `.\.venv\Scripts\python.exe -m jobs.sync_google_sheets_history --sheet 2021 --sheet 2022 --sheet 2023 --sheet 2024 --sheet 2025 --sheet 2026 --replay-confirmed-progress`
- [x] `viewing_history` now stores `douban_subject_id` directly.
- [x] `viewing_history.movie_id` is nullable and can be backfilled after `movies` rows exist.
- [x] The corrected `--replay-confirmed-progress` path only writes `viewing_history`; it does not fetch Douban detail pages and does not write `movies`.
- [ ] The next detail-loading step should read distinct `viewing_history.douban_subject_id`, fetch Douban details, upsert `movies`, backfill `viewing_history.movie_id`, and enqueue recommendations.
- [x] Corrected dry-run still verifies:
  - Google Sheets rows read from `2021-2026`: 498.
  - Progress JSON items: 893.
  - Confirmed progress source rows available for replay after skipping conflicts: 449.
  - Same-priority confirmed subject-id conflicts: 7.
- Confirmed conflicts currently skipped:
  - `2024` row 24: `auto_matched_persisted` subjects `25980443`, `35662198`.
  - `2023` row 59: `manual_id_persisted` subjects `35814636`, `26427445`.
  - `2023` row 75: `auto_matched_persisted` subjects `35660795`, `37233270`.
  - `2022` row 57: `manual_id_persisted` subjects `1299112`, `33464080`.
  - `2022` row 93: `manual_id_persisted` subjects `30198955`, `2373195`.
  - `2021` row 9: `manual_id_persisted` subjects `1291568`, `26789753`.
  - `2021` row 113: `manual_id_persisted` subjects `37825410`, `25966044`.
- [x] Tests passed after correction:
  - `.\.venv\Scripts\python.exe -m unittest backend.tests.test_sqlite_repository backend.tests.test_sync_google_sheets_history_job backend.tests.test_history_persistence_service`
  - `.\.venv\Scripts\python.exe -m unittest discover -s backend\tests`
- [x] Dry-run verified without writing:
  - `.\.venv\Scripts\python.exe -m jobs.sync_google_sheets_history --sheet 2021 --sheet 2022 --sheet 2023 --sheet 2024 --sheet 2025 --sheet 2026 --replay-confirmed-progress --dry-run`
- Dry-run result:
  - Google Sheets rows read from `2021-2026`: 498.
  - Progress JSON items: 893.
  - Confirmed progress source rows available for replay after skipping conflicts: 449.
  - Same-priority confirmed subject-id conflicts: 7.
- [x] `Sheet1` exists in old progress JSON but current Google Sheets returns HTTP 400 for that tab/range; it is excluded from the prepared import command unless the user confirms another sheet name.
- [x] Tests passed:
  - `.\.venv\Scripts\python.exe -m unittest backend.tests.test_sync_google_sheets_history_job`
  - `.\.venv\Scripts\python.exe -m unittest backend.tests.test_import_auto_matched_history_job`
  - `.\.venv\Scripts\python.exe -m unittest backend.tests.test_candidate_pool_job`
  - `.\.venv\Scripts\python.exe -m unittest discover -s backend\tests`
  - Latest full run: `Ran 116 tests OK (skipped=3)`.

## Current PostgreSQL State After Viewing-History Replay

- [x] Confirmed replay executed:
  - `.\.venv\Scripts\python.exe -m jobs.sync_google_sheets_history --sheet 2021 --sheet 2022 --sheet 2023 --sheet 2024 --sheet 2025 --sheet 2026 --replay-confirmed-progress`
- Replay summary:
  - Google Sheets imported rows: 486.
  - Skipped invalid rows: 12.
  - Confirmed progress source rows: 456.
  - Direct sheet subject-id rows: 26.
  - Matched confirmed rows: 441.
  - Skipped without subject id: 19.
  - Persisted viewing-history rows: 467.
  - Failed rows: 0.
- Post-replay database state:
  - `viewing_history`: 467 rows.
  - Distinct `viewing_history.douban_subject_id`: 463.
  - `viewing_history.movie_id` populated rows: 0; this is expected before movie detail loading.
  - Missing `douban_subject_id`: 0.
  - `movies`: 0 rows.
  - `candidate_subject_queue`: 254 rows.
  - `candidate_pool`: 0 rows.
- Rows by source sheet:
  - `2021`: 154.
  - `2022`: 95.
  - `2023`: 79.
  - `2024`: 52.
  - `2025`: 61.
  - `2026`: 26.

## Viewing-History Replay Gap Analysis

- [x] Explained the gap from 498 Google Sheets rows to 467 persisted `viewing_history` rows.
- Gap formula:
  - 498 Google Sheets rows
  - minus 12 invalid rows
  - minus 19 valid rows without direct/progress subject id
  - equals 467 persisted `viewing_history` rows.
- Invalid rows are rows with missing or non-numeric rating, mostly `Rating='/'` plus one blank rating.
- Valid rows skipped without subject id are rows that had enough viewing data but no direct `movie_id` / `DoubanSubjectId` in Google Sheets and no confirmed progress match.

## Movie Rebuild From Viewing History Preparation

- [x] Added `jobs/rebuild_movies_from_history.py`.
- Job behavior:
  - Reads distinct `viewing_history.douban_subject_id` values that are missing from `movies`.
  - Fetches Douban detail pages.
  - Upserts `movies`.
  - Backfills `viewing_history.movie_id`.
  - Parses detail-page recommendations and inserts them into `candidate_subject_queue`.
- [x] Added repository helpers:
  - `find_history_subject_ids_missing_movies(limit=None)`.
  - `backfill_viewing_history_movie_id(douban_subject_id, movie_id)`.
- [x] Dry-run verified without fetching or writing:
  - `.\.venv\Scripts\python.exe -m jobs.rebuild_movies_from_history --dry-run --limit 5`
- Dry-run result:
  - Pending history subjects missing from `movies`: 463.
  - First selected subject IDs: `1296339`, `1302642`, `1306029`, `1291999`, `1293359`.
- [x] Tests passed:
  - `.\.venv\Scripts\python.exe -m unittest backend.tests.test_rebuild_movies_from_history_job backend.tests.test_sqlite_repository backend.tests.test_candidate_pool_job`
  - `.\.venv\Scripts\python.exe -m unittest discover -s backend\tests`
  - Latest full run: `Ran 118 tests OK (skipped=3)`.
- Pending real command, not yet run:
  - `.\.venv\Scripts\python.exe -m jobs.rebuild_movies_from_history`

## Documentation Updates

- [x] Updated `README.md` to document the current split rebuild flow:
  - Google Sheets + confirmed progress JSON rebuilds `viewing_history` only.
  - `jobs.rebuild_movies_from_history` rebuilds `movies`, backfills `viewing_history.movie_id`, and enqueues recommendations.
  - Removed stale `MOVIES.xlsx#2026` / `--source-file-alias` guidance from the main Google Sheets path.
- [x] Updated `docs/architecture.md` data flow and `viewing_history` schema:
  - `douban_subject_id` is required on `viewing_history`.
  - `movie_id` is nullable backfill cache.
  - Current jobs include `sync_google_sheets_history.py` and `rebuild_movies_from_history.py`.
- [x] Updated `CONTEXT.md` with the corrected two-step rebuild contract and confirmed-status priority.
- [x] Checked for stale wording in README / architecture / context.

## Incremental Update Contract

- [x] `jobs.sync_google_sheets_history` now defaults to reading every sheet tab from Google Sheets metadata; `--sheet` is optional and only needed to restrict a run.
- [x] Incremental record-watched path checked and adjusted:
  - Append the watched row to Google Sheets.
  - Upsert `viewing_history` with `douban_subject_id`.
  - Fill `movie_id` immediately only when `movies` already has that subject.
  - If `movies` does not have the watched subject, synchronously fetch that watched movie's Douban detail, write `movies`, and fill `viewing_history.movie_id`.
  - Do not put the watched movie itself into `candidate_subject_queue` / `candidate_pool`.
  - Insert detail-page recommendations into `candidate_subject_queue` when page source is available.
- [x] Background follow-up for incremental updates:
  - `jobs.candidate_pool process-queue` later turns queued recommended subjects into `movies` and active `candidate_pool` entries.
