# Personal Movie Recommender

A local, single-user movie recommendation system for choosing what to watch on demand.

The product direction is documented in [CONTEXT.md](CONTEXT.md), with detailed requirements in [docs/requirements.md](docs/requirements.md) and architecture notes in [docs/architecture.md](docs/architecture.md).

## Current Status

The current application is a local Electron desktop app backed by React, FastAPI, PostgreSQL, Google Sheets, and offline Douban enrichment jobs.

Implemented workflows:

- request eight recommendations: four exploit and four explore
- choose `hybrid` or `bandit_hybrid` in the UI (the default remains `hybrid`); the API also supports `popularity` and `content`
- record want-to-watch, maybe-later, and not-interested feedback
- manage wishlist and not-interested state
- search for a movie and record it as watched
- browse, edit, and remove viewing-history records
- persist watched records in PostgreSQL with a retryable Google Sheets sync outbox
- synchronously create missing canonical watched movies
- build and enrich a resumable local recommendation candidate pool
- run the same UI in a browser during development or in an Electron desktop window on Windows or macOS

With the PostgreSQL backend selected, recommendation generation reads local data only. Other interactive paths may call Douban for title search or missing watched-movie metadata, and may call Google Sheets to flush viewing-history changes. Poster images load from Douban image hosts.

## Project Layout

```text
backend/
  app/
    api/             FastAPI routes
    models/          Domain models
    recommenders/    Baseline and bandit scoring logic
    db/              PostgreSQL and SQLite viewing-history repositories
    services/        Application services and recommendation repositories
  tests/             Backend unit tests
docs/                Requirements, architecture, and agent workflow docs
frontend/            React UI for recommendations, search, and recording history
desktop/             Electron lifecycle, preload bridge, and request policy
jobs/                Google Sheets sync, rebuild, enrichment, and evaluation jobs
```

## Setup

Create and activate a virtual environment on macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

On Windows PowerShell:

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
$env:MOVIES_RECOMMENDATION_BACKEND="memory"
.\.venv\Scripts\python.exe -m unittest discover -s backend\tests
Remove-Item Env:\MOVIES_RECOMMENDATION_BACKEND
```

On macOS or Linux, use `MOVIES_RECOMMENDATION_BACKEND=memory .venv/bin/python -m unittest discover -s backend/tests`. This keeps the unit suite independent of a local PostgreSQL connection.
Some PostgreSQL integration tests skip unless a test DSN is configured. To run them, create a separate test database and set:

```powershell
$env:MOVIES_POSTGRES_DSN="postgresql://user:password@localhost:5432/movies_test"
.\.venv\Scripts\python.exe -m unittest backend.tests.test_postgres_repository
```

## Run As A Desktop App

The desktop app uses Electron as a native window around the existing React frontend and FastAPI backend. It reuses the local `.venv` backend, so the normal Python setup above is still required.

For a PostgreSQL-backed Windows build, install PostgreSQL locally, create an empty database, and put its connection string and backend selection in the local `.env`:

```text
MOVIES_RECOMMENDATION_BACKEND=postgres
MOVIES_POSTGRES_DSN=postgresql://user:password@localhost:5432/movies
```

Initialize that database before starting the API for the first time:

```powershell
.\.venv\Scripts\python.exe -m jobs.init_database
```

The command uses the versioned schema definition in `backend/app/db/postgres_repository.py` and can be run again. `start-app.cmd` and `start-dev.ps1` run it automatically before the API when the PostgreSQL backend is selected. PostgreSQL must already be running. The in-memory development backend skips this step.

Install the desktop dependencies once:

```powershell
Push-Location frontend
npm install
Pop-Location

Push-Location desktop
npm install
Pop-Location
```

Build the frontend once:

```powershell
Push-Location frontend
npm run build
Pop-Location
```

Then double-click `start-app.cmd` from File Explorer, or run:

```powershell
.\start-app.cmd
```

The Electron window starts the backend automatically on `127.0.0.1:8000`, loads the built frontend, and shuts down the backend when the app window closes.

For a Windows build with the real Mac dataset, restore a Mac PostgreSQL custom-format backup into a **new, empty Windows-local database**, then point Windows `.env` at that database. Keep the Mac database as the authoritative copy; do not copy PostgreSQL's data directory between operating systems. See [docs/desktop.md](docs/desktop.md#moving-a-mac-database-to-windows-for-testing) for the restore and smoke-check steps.

On macOS, install dependencies with `npm --prefix frontend ci` and `npm --prefix desktop ci`, build with `npm --prefix frontend run build`, then run `npm --prefix desktop start` from the repository root. The local `Movies.app` launcher also starts Homebrew `postgresql@16` before opening the desktop shell. Both paths use `.venv/bin/python`; they still require local PostgreSQL, Chrome, and application configuration. See [docs/mac-codex-build-notes.md](docs/mac-codex-build-notes.md) for the existing Mac setup.

Desktop mode also prewarms the shared headless Selenium driver in the background so the first Add watched submission for a missing canonical movie does not pay the Chrome startup cost. To disable that behavior for a run:

```powershell
$env:MOVIES_PREWARM_RECORD_SELENIUM="0"
.\start-app.cmd
```

See [docs/desktop.md](docs/desktop.md) for lifecycle behavior, performance notes, and troubleshooting.

## Import And Rebuild Viewing History

The historical rebuild path reads Google Sheets and the confirmed progress file. In the running application, PostgreSQL is the source of truth and Google Sheets is a one-way projection. Local `.xlsx` files are legacy snapshots unless explicitly needed for older review workflows.

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

The application header shows a `Process queue (N)` button whose count includes both pending and failed rows. It processes both statuses in database queue order on a background thread and closes its dedicated Selenium driver when processing ends. Processing stops on the first failed item and cannot be restarted until the app is reopened. Debug mode adds a compact details card with the current subject and failure reason.

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

- `GET /candidate-queue/status`
- `POST /candidate-queue/process`
- `GET /movies/search?q=Still%20Walking`
- `POST /viewing-history`
- `GET /viewing-history?year=2026&limit=50&offset=0&order=desc`
- `PATCH /viewing-history/{history_id}`
- `DELETE /viewing-history/{history_id}`
- `POST /viewing-history/{history_id}/sync`
- `GET /viewing-history-sync/status`
- `GET /recommendations?strategy=hybrid`
- `GET /recommendations?strategy=bandit_hybrid`
- `GET /recommendations?strategy=hybrid&seed=42`
- `GET /recommendations?strategy=popularity`
- `GET /recommendations?strategy=content`
- `GET /recommendations/{session_id}`
- `POST /recommendations/{session_id}/items/{item_id}/feedback`
- `DELETE /recommendations/{session_id}/items/{item_id}/processing`
- `GET /wishlist`
- `POST /wishlist/{wishlist_id}/watched`
- `DELETE /wishlist/{wishlist_id}`
- `GET /not-interested`
- `DELETE /not-interested/{movie_id}`

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

The API commits `viewing_history` and its Sheet-sync outbox task locally, then attempts an immediate Google Sheets flush. A Sheet failure leaves the local history record saved and the pending task retryable. If `movies` already has the selected subject, `movie_id` is filled immediately. If not, the request synchronously fetches the watched movie detail, writes `movies`, and fills `viewing_history.movie_id`. Detail-page recommendations are inserted into `candidate_subject_queue`; recommended candidates are later enriched and activated by `jobs.candidate_pool process-queue`.

Deleting a managed viewing-history record soft-deletes it in PostgreSQL and removes its Google Sheets projection. Its existing candidate-pool row is reactivated only if the movie has no other active viewing-history record and no active wishlist entry. A current effective `not_interested` state still excludes the movie from recommendations.

## Run The Frontend

Start the API first, then run the React UI:

```powershell
npm --prefix frontend install
npm --prefix frontend run dev
```

The dev server proxies `/movies`, `/viewing-history`, `/recommendations`, `/wishlist`, and `/not-interested` to `http://127.0.0.1:8000`. Its current proxy does not include `/candidate-queue`; the browser queue control needs `VITE_API_BASE_URL=http://127.0.0.1:8000` or a proxy update. Use the Vite URL printed by `npm run dev`, usually `http://127.0.0.1:5173/`.

To start both the API and frontend in separate PowerShell windows:

```powershell
.\start-dev.cmd
```

Use `start-dev.cmd` if launching from File Explorer; double-clicking a `.ps1`
file may open it in an editor instead of running it.

## Recommendation Scoring

The baseline scoring rules live in `backend/app/recommenders/simple.py`. `backend/app/recommenders/bandit.py` implements the optional learning strategy.

The implementation provides:

- `popularity_score`: combines Douban rating and log-scaled vote count.
- `content_score`: builds positive and negative feature profiles from viewing history ratings.
- `hybrid_score`: combines personal preference, public quality, and a small novelty bonus.
- `diversity_gain`: batch-local diversity for the four explore slots.

See [docs/architecture.md](docs/architecture.md#recommendation-strategy) for the exact formulas and the 4 exploit / 4 explore selection rule.

`bandit_hybrid` keeps the four hybrid exploit slots and uses diagonal Linear Thompson Sampling for exploration after at least 20 trainable examples. Before that threshold, or if training fails, it uses the hybrid explore selector. The UI lets users select either strategy and remembers the choice locally; a fresh install starts with `hybrid`.

To inspect recommendation output quality against PostgreSQL data, run:

```powershell
.\.venv\Scripts\python.exe -m jobs.evaluate_recommendations --strategy hybrid --runs 10
```

To make explore-slot randomness reproducible while evaluating, pass a seed:

```powershell
.\.venv\Scripts\python.exe -m jobs.evaluate_recommendations --strategy hybrid --runs 10 --seed 42
.\.venv\Scripts\python.exe -m jobs.evaluate_recommendations --strategy bandit_hybrid --runs 10 --seed 42
```

The report prints:

- `candidate_pool_health`: active pool size, recommendation-eligible size, queue status counts, active source mix, and missing metadata counts.
- recommendation runs: each returned item with slot type, score, rating, watched flag, and pool source.
- `summary`: unique movies, slot mix, source mix, repeated movies, duplicate items within a session, and watched-movie leakage.
- for `bandit_hybrid`: trainable-example count, bandit use/fallback counts, and fallback reasons.

Use this as the daily check after each candidate-pool batch. The first hard gates are `watched_leak_count=0`, `duplicate_in_session_count=0`, and `eligible_unique_movies >= 5`; after the pool grows, watch whether `repeated_movies` and `active_source_mix` show over-concentration.

## Project Documents

- [CONTEXT.md](CONTEXT.md): product boundaries and domain decisions
- [docs/requirements.md](docs/requirements.md): current functional requirements
- [docs/architecture.md](docs/architecture.md): data flow, persistence, APIs, and recommendation mechanics
- [docs/contextual-bandit-design.md](docs/contextual-bandit-design.md): `bandit_hybrid` strategy, Linear Thompson Sampling design, and reward rules
- [docs/checklists/contextual-bandit-implementation-checklist.md](docs/checklists/contextual-bandit-implementation-checklist.md): completed backend implementation record for `bandit_hybrid`
- [docs/desktop.md](docs/desktop.md): Electron runtime and lifecycle
- [docs/technical-debt.md](docs/technical-debt.md): prioritized correctness, security, and maintainability risks
- [docs/checklists/frontend-performance-checklist.md](docs/checklists/frontend-performance-checklist.md): completed frontend slices and remaining performance work
- [docs/checklists/database-rebuild-checklist.md](docs/checklists/database-rebuild-checklist.md): database rebuild history and verification
