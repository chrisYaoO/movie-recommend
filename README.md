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
Ran 52 tests

OK (skipped=2)
```

The skipped tests are optional PostgreSQL integration tests. To run them, install PostgreSQL, create a test database, and set:

```powershell
$env:MOVIES_POSTGRES_DSN="postgresql://user:password@localhost:5432/movies_test"
.\.venv\Scripts\python.exe -m unittest backend.tests.test_postgres_repository
```

## Import Auto-Matched History

The current import job only persists automatically matched records. `needs_review` and `no_match` rows are counted and skipped; the manual review flow is intentionally left for a later slice.

The CLI reads PostgreSQL connection settings from `--dsn`, `MOVIES_POSTGRES_DSN`, or local `.env`, in that order. A local `.env` should contain:

```text
MOVIES_POSTGRES_DSN=postgresql://user:password@localhost:5432/movies
```

```powershell
.\.venv\Scripts\python.exe -m jobs.import_auto_matched_history data\imports\MOVIES.xlsx
```

For the first safe import from the current workbook, use only rows that already carry a Douban subject id:

```powershell
.\.venv\Scripts\python.exe -m jobs.import_auto_matched_history data\imports\MOVIES.xlsx --subject-id-only --detail-adapter selenium --chrome-binary-path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

To process the remaining metadata-search rows safely, use the resumable mode. It searches one row at a time, persists each `AUTO_MATCHED` result immediately, writes progress to `data/cache/import-auto-match-progress.json`, and exits on the first error so the next run can resume from that node.

```powershell
.\.venv\Scripts\python.exe -m jobs.import_auto_matched_history data\imports\MOVIES.xlsx --metadata-search-resume --detail-adapter selenium --chrome-binary-path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```（

To manually review `needs_review` rows from the progress file, run:

```powershell
.\.venv\Scripts\python.exe -m jobs.review_matched_history data\imports\MOVIES.xlsx --detail-adapter selenium --chrome-binary-path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

For each row, press `Enter` to queue it for confirmation, enter `1` to reject it, or enter `q` to stop and resume later. The prompt only shows data already in the progress JSON so review stays fast; when the review loop exits, queued confirmations fetch Douban detail pages and persist the detail-page title as the canonical movie title.

## Build Candidate Pool

Queue Douban Top250 subject IDs with `source_type=douban_top250` and `source_ref=top{rank}`:

```powershell
.\.venv\Scripts\python.exe -m jobs.candidate_pool discover-top250
```

Process queued subjects, enrich missing movie details, add movies to `candidate_pool`, and enqueue one layer of "recommended from" subjects:

```powershell
.\.venv\Scripts\python.exe -m jobs.candidate_pool process-queue --limit 25 --chrome-binary-path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

The queue is resumable through PostgreSQL statuses. `movies` stores canonical metadata only; watched, wishlist, feedback, and candidate eligibility remain in separate tables.

## Run The API

After installing dependencies:

```powershell
uvicorn backend.app.main:app --reload
```

Useful endpoints in the current slice:

- `GET /recommendations?strategy=hybrid`
- `GET /recommendations?strategy=popularity`
- `GET /recommendations?strategy=content`
- `POST /recommendations/{session_id}/items/{item_id}/feedback`
- `GET /wishlist`
- `POST /wishlist/{wishlist_id}/watched`

## Next Tasks

Recommended next implementation order:

1. Run the auto-matched import job against a real PostgreSQL database and inspect persisted data quality.
2. Build the local candidate pool tables and ingestion path.
3. Add the manual Douban match review queue and API.
4. Switch recommendation reads from the in-memory sample catalog to persisted movies/candidates.
5. Add the React recommendation and wishlist UI.
