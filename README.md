# Personal Movie Recommender

A local, single-user movie recommendation system for choosing what to watch on demand.

The product direction is documented in [CONTEXT.md](CONTEXT.md), with detailed requirements in [docs/requirements.md](docs/requirements.md) and architecture notes in [docs/architecture.md](docs/architecture.md).

## Current Status

This repository currently contains the first backend vertical slice:

- core movie, feedback, wishlist, and recommendation session domain models
- an in-memory repository seeded with sample movie data
- popularity, content-based, and hybrid recommendation scoring
- on-demand recommendation sessions that return exactly five movies
- three exploit slots and two explore slots per recommendation session
- feedback handling for `want_to_watch`, `maybe_later`, `not_interested`, and `opened_douban`
- wishlist creation from `want_to_watch`
- recording a wishlist movie as watched
- raw viewing-history import from the confirmed Excel column shape
- `.xlsx` reading through `openpyxl`
- stable raw-row hashes so repeated imports can skip duplicates
- Douban match input generation, confidence scoring, and manual subject-id confirmation
- Douban subject detail parsing and Selenium-backed enrichment job support
- SQLite persistence for canonical `movies` and final `viewing_history`
- PostgreSQL repository support for the same `movies` and `viewing_history` contract
- PostgreSQL-backed recommendation reads from `movies`, `candidate_pool`, and `viewing_history`
- focused unit tests for the core recommendation loop

This slice still keeps live recommendation independent from Douban. External access belongs in import and enrichment jobs only.

## Project Layout

```text
backend/
  app/
    api/             FastAPI routes
    models/          Domain models
    recommenders/    Baseline scoring logic
    services/        Application services and in-memory repository
  tests/             Backend unit tests
docs/                Requirements, architecture, and agent workflow docs
frontend/            React UI for recommendations, search, and recording history
```

## Setup

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements-dev.txt
```

## Run Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s backend\tests
```

Expected result:

```text
Ran 118 tests

OK (skipped=3)
```

The skipped tests are optional PostgreSQL integration tests. To run them, install PostgreSQL, create a test database, and set:

```powershell
$env:MOVIES_POSTGRES_DSN="postgresql://user:password@localhost:5432/movies_test"
.\.venv\Scripts\python.exe -m unittest backend.tests.test_postgres_repository
```

## Import And Rebuild Viewing History

The current rebuild path treats Google Sheets as the source of truth for viewing history. Local `.xlsx` files are legacy snapshots unless explicitly needed for older review workflows.

The CLI reads PostgreSQL connection settings from `--dsn`, `MOVIES_POSTGRES_DSN`, or local `.env`, in that order. A local `.env` should contain:

```text
MOVIES_POSTGRES_DSN=postgresql://user:password@localhost:5432/movies
```

By default the Google Sheets job reads service account credentials from `.secrets/google-sheets-service-account.json`.
The spreadsheet id should be stored in that JSON as `spreadsheet_id`; `.env` does not need a Google Sheets id for the normal path.

To verify Google Sheets access without writing to PostgreSQL or fetching Douban details, run:

```powershell
.\.venv\Scripts\python.exe -m jobs.sync_google_sheets_history --replay-confirmed-progress --dry-run
```

To rebuild `viewing_history` from Google Sheets plus the existing confirmed progress JSON, run:

```powershell
.\.venv\Scripts\python.exe -m jobs.sync_google_sheets_history --replay-confirmed-progress
```

This command only writes `viewing_history`. It does not fetch Douban detail pages, does not write `movies`, and does not enqueue recommendations. It stores `douban_subject_id` directly on `viewing_history`; `movie_id` is nullable and is backfilled later after `movies` rows exist.

Subject IDs are resolved in this order:

1. Direct `movie_id` / `DoubanSubjectId` value in the sheet row.
2. Confirmed `data/cache/import-auto-match-progress.json` entry matched by `(source_sheet_name, source_row_number)`.
3. Confirmed progress entry matched by `source_row_checksum` as a fallback.

Confirmed progress statuses use this priority:

```text
manual_id_persisted > review_confirmed_persisted > auto_matched_persisted
```

If the highest-priority confirmed entries for the same source row disagree on subject ID, the row is reported as a conflict and skipped.

By default the sync job reads every sheet tab in the spreadsheet metadata. Pass one or more `--sheet` arguments only when you want to restrict the run to specific tabs. A Google Sheets row from tab `2026` is stored with `source_sheet_name=2026` and its original sheet row number. A row-content checksum is stored for change detection, but uniqueness comes from `source_sheet_name + source_row_number`.

Share the Google Sheet with the `client_email` from the service account JSON. Use Editor access if this service account will later write back to the sheet. If you are syncing a public sheet, you can use an API key instead:

```text
GOOGLE_SHEETS_SPREADSHEET_ID=your-spreadsheet-id
GOOGLE_SHEETS_API_KEY=your-api-key
```

After `viewing_history` is rebuilt, rebuild canonical movie metadata from the distinct watched subject IDs:

```powershell
.\.venv\Scripts\python.exe -m jobs.rebuild_movies_from_history
```

Use `--limit N` for a smaller real batch, or `--dry-run` to list selected subject IDs without fetching or writing:

```powershell
.\.venv\Scripts\python.exe -m jobs.rebuild_movies_from_history --limit 5
.\.venv\Scripts\python.exe -m jobs.rebuild_movies_from_history --dry-run --limit 5
```

The movie rebuild job fetches Douban detail pages, upserts `movies`, backfills `viewing_history.movie_id`, and enqueues one layer of Douban recommendations into `candidate_subject_queue`. Selenium jobs default to Chrome at `C:\Program Files\Google\Chrome\Application\chrome.exe`; pass `--chrome-binary-path` only when using a different Chrome executable.

The older workbook importer still exists for legacy review and repair workflows. It reads a local workbook, uses resumable checkpoints in `data/cache/import-auto-match-progress.json`, and can fetch details while persisting confirmed rows:

```powershell
.\.venv\Scripts\python.exe -m jobs.import_auto_matched_history data\imports\MOVIES.xlsx --detail-adapter selenium
```

To reclassify older `no_match` checkpoint rows whose only blocker was `douban_search_no_year_match`, run:

```powershell
.\.venv\Scripts\python.exe -m jobs.import_auto_matched_history data\imports\MOVIES.xlsx --retry-no-year-match-no-matches
```

This updates matching state only. Rows with search results move back to `needs_review`; only true search misses stay `no_match`.

To manually review `needs_review` rows from the progress file, run:

```powershell
.\.venv\Scripts\python.exe -m jobs.review_matched_history data\imports\MOVIES.xlsx --detail-adapter selenium
```

For each row, press `Enter` to queue it for confirmation, enter `1` to reject it, or enter `q` to stop and resume later. The prompt only shows data already in the progress JSON so review stays fast; when the review loop exits, queued confirmations fetch Douban detail pages and persist the detail-page title as the canonical movie title.

To manually resolve `review_rejected` and `no_match` rows by entering a Douban subject id, run:

```powershell
.\.venv\Scripts\python.exe -m jobs.review_matched_history data\imports\MOVIES.xlsx --resolve-rejected-and-no-match --detail-adapter selenium
```

For each row, enter a Douban subject id or subject URL to fetch and preview the detail page, then press `Enter` to persist it or enter `1` to discard it. Enter `a` to run a fresh Douban search, without reusing the progress JSON or local search cache, and update the row to `auto_matched_persisted`, `needs_review`, or `no_match` under the current matching rules. Enter `x` at the subject-id prompt to discard without fetching, or `q` to stop and resume later.

To run that fresh Douban search retry in batch for `review_rejected` and `no_match` rows, run:

```powershell
.\.venv\Scripts\python.exe -m jobs.review_matched_history data\imports\MOVIES.xlsx --batch-search-rejected-and-no-match --detail-adapter selenium
```

Use `--limit N` to process a smaller batch. Each row prints the fresh search status and candidate subject id/title/year.

## Build Candidate Pool

Queue Douban Top250 subject IDs with `source_type=douban_top250` and `source_ref=top{rank}`:

```powershell
.\.venv\Scripts\python.exe -m jobs.candidate_pool discover-top250
```

Queue one layer of Douban recommendations from every unprocessed watched movie:

```powershell
.\.venv\Scripts\python.exe -m jobs.candidate_pool discover-history-recommendations
```

This command records completed watched movies in `history_recommendation_discovery`, so it can resume from the remaining unprocessed viewing-history movies. Failed watched movies stay unprocessed and are retried on the next run. Use `--limit N` only when you want a smaller batch:

```powershell
.\.venv\Scripts\python.exe -m jobs.candidate_pool discover-history-recommendations --limit 25
```

Process queued subjects, enrich missing movie details, add movies to `candidate_pool`, and enqueue one layer of "recommended from" subjects:

```powershell
.\.venv\Scripts\python.exe -m jobs.candidate_pool process-queue
```

Use `--limit N` to process a different batch size:

```powershell
.\.venv\Scripts\python.exe -m jobs.candidate_pool process-queue --limit 50
```

To retry failed queue rows, run:

```powershell
.\.venv\Scripts\python.exe -m jobs.candidate_pool process-queue --retry-failed --limit 25
```

The queue is resumable through PostgreSQL statuses. `movies` stores canonical metadata only; watched, wishlist, feedback, and candidate eligibility remain in separate tables.

## Run The API

After installing dependencies:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
```

By default the API uses the in-memory seed catalog. To read recommendation candidates from PostgreSQL, first build/import `movies` and `candidate_pool`, then add these settings to local `.env`:

```powershell
MOVIES_RECOMMENDATION_BACKEND=postgres
MOVIES_POSTGRES_DSN=postgresql://user:password@localhost:5432/movies
```

The backend loads `.env` automatically at startup, while real process environment variables still take precedence for temporary overrides.

Useful endpoints in the current slice:

- `GET /movies/search?q=Still%20Walking`
- `POST /viewing-history`
- `GET /recommendations?strategy=hybrid`
- `GET /recommendations?strategy=hybrid&seed=42`
- `GET /recommendations?strategy=popularity`
- `GET /recommendations?strategy=content`
- `POST /recommendations/{session_id}/items/{item_id}/feedback`
- `GET /wishlist`
- `POST /wishlist/{wishlist_id}/watched`

To record a watched movie selected from search:

```json
{
  "douban_subject_id": "2222996",
  "watched_date": "2026-05-26",
  "rating": 4.5,
  "quality": "1080p",
  "comment": "quietly great",
  "sheet": "2026"
}
```

The API appends the row to Google Sheets first, then writes `viewing_history` with `source_sheet_name=<sheet>`, the appended sheet row number, and `douban_subject_id`. If `movies` already has that subject, `movie_id` is filled immediately. If not, the request synchronously fetches the watched movie detail, writes `movies`, and fills `viewing_history.movie_id`. Detail-page recommendations are inserted into `candidate_subject_queue`; recommended candidates are later enriched and activated by `jobs.candidate_pool process-queue`.

## Run The Frontend

Start the API first, then run the React UI:

```powershell
cd frontend
npm install
npm run dev
```

The dev server proxies API requests to `http://127.0.0.1:8000`, so the browser should use the Vite URL printed by `npm run dev`, usually `http://127.0.0.1:5173/`.

## Recommendation Scoring

The current baseline scoring rules live in `backend/app/recommenders/simple.py`.

The implementation provides:

- `popularity_score`: combines Douban rating and log-scaled vote count.
- `content_score`: builds positive and negative feature profiles from viewing history ratings.
- `hybrid_score`: combines personal preference, public quality, and a small novelty bonus.
- `diversity_gain`: batch-local diversity for the two explore slots.

See [docs/architecture.md](docs/architecture.md#recommendation-strategy) for the exact formulas and the 3 exploit / 2 explore selection rule.

To inspect recommendation output quality against PostgreSQL data, run:

```powershell
.\.venv\Scripts\python.exe -m jobs.evaluate_recommendations --strategy hybrid --runs 10
```

To make explore-slot randomness reproducible while evaluating, pass a seed:

```powershell
.\.venv\Scripts\python.exe -m jobs.evaluate_recommendations --strategy hybrid --runs 10 --seed 42
```

The report prints:

- `candidate_pool_health`: active pool size, recommendation-eligible size, queue status counts, active source mix, and missing metadata counts.
- recommendation runs: each returned item with slot type, score, rating, watched flag, and pool source.
- `summary`: unique movies, slot mix, source mix, repeated movies, duplicate items within a session, and watched-movie leakage.

Use this as the daily check after each candidate-pool batch. The first hard gates are `watched_leak_count=0`, `duplicate_in_session_count=0`, and `eligible_unique_movies >= 5`; after the pool grows, watch whether `repeated_movies` and `active_source_mix` show over-concentration.

## Next Tasks

Recommended next implementation order:

1. Wire the React UI to the PostgreSQL-backed API in a local end-to-end run.
2. Add the final "record watched" validation states around Google Sheets write failures and duplicate local rows.
3. Persist wishlist and feedback against PostgreSQL instead of keeping them process-local.
4. Continue candidate-pool enrichment and use `jobs.evaluate_recommendations` as the daily health check.
