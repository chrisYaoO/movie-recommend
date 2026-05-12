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
- focused unit tests for the core recommendation loop

This is intentionally not connected to PostgreSQL or Douban yet. The current slice fixes the behavior contract before persistence and ingestion are added.

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
Ran 3 tests

OK
```

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

1. Excel viewing-history import with stable row hashes and duplicate prevention.
2. PostgreSQL schema and repository layer.
3. Douban matching review queue.
4. Metadata enrichment and local candidate pool.
5. React frontend for recommendations and wishlist.
