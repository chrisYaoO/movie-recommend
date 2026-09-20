# Personal Movie Recommendation System Architecture

## System Shape

The system has five main parts:

1. data jobs
   - Google Sheets viewing-history sync
   - Douban matching
   - Douban metadata enrichment
   - candidate pool construction
2. PostgreSQL
   - system of record for viewing history, movies, feedback, wishlist, and recommendation sessions
3. FastAPI backend
   - recommendation API
   - feedback API
   - viewing-history management and candidate-queue control APIs
4. React frontend
   - recommendation workflow
   - wishlist workflow
   - Add watched workflow
   - not-interested workflow
5. Electron desktop shell
   - starts and stops the local FastAPI backend
   - loads the built React frontend in a native window
   - injects required poster request headers
   - keeps desktop-only process lifecycle behavior outside the web frontend

Live recommendation must not call Douban. It only reads PostgreSQL.

The active application database runs locally on the Mac. Windows is a build target, not a second synchronized runtime; a future Windows installation with real data requires its own PostgreSQL database restored from backup. See [ADR 0001](adr/0001-local-mac-database-no-cross-device-sync.md).

The Add watched workflow fetches missing canonical watched-movie metadata synchronously, then commits locally. Google Sheets is updated best-effort after the local commit; its availability never determines whether the history mutation succeeds.

## Data Flow

```text
historical Google Sheets viewing history
+ existing confirmed progress JSON
-> viewing_history with stable UUIDs and Douban subject IDs
-> movie metadata rebuild from distinct viewing_history subject IDs
-> movies + viewing_history.movie_id backfill
-> one-layer recommendation queue from watched detail pages
-> recommender
-> recommendation session + recommendation items
-> frontend feedback
-> local viewing_history updates + sheet_sync_outbox
-> Google Sheets projection with hidden RecordId
```

Candidate ingestion follows a related path:

```text
Douban list/search/similar-source import
-> candidate subject IDs
-> Douban subject enrichment
-> movies
-> candidate_pool entries
```

The desktop interactive path is:

```text
start-app.cmd (Windows; initialize PostgreSQL schema first when selected)
or npm --prefix desktop start / Movies.app (macOS)
-> Electron window + FastAPI backend in parallel
-> frontend waits for backend through preload IPC only when making API calls
-> background Selenium prewarm for missing watched-movie metadata
-> close window
-> stop the backend child and close the shared Selenium driver
```

## Module Layout

Current repository layout (selected paths):

```text
movies/
|-- CONTEXT.md
|-- docs/
|   |-- requirements.md
|   `-- architecture.md
|-- backend/
|   |-- app/
|   |   |-- api/
|   |   |-- db/
|   |   |-- models/
|   |   |-- services/
|   |   `-- recommenders/
|   `-- tests/
|-- jobs/
|   |-- sync_google_sheets_history.py
|   |-- import_auto_matched_history.py
|   |-- rebuild_movies_from_history.py
|   `-- candidate_pool.py
|-- frontend/
|   `-- src/
|-- desktop/
|   |-- main.cjs
|   |-- preload.cjs
|   `-- launch.cjs
`-- data/
    |-- imports/
    `-- cache/
```

## PostgreSQL Schema

The table summaries below reflect the schema created by `PostgresViewingHistoryRepository.initialize_schema()` in `backend/app/db/postgres_repository.py`. `python -m jobs.init_database` invokes that versioned code before a Windows PostgreSQL-backed app starts; `start-app.ps1` and `start-dev.ps1` call it automatically. There is no separate `schema.sql` or complete drift checker. Legacy import and matching structures belong to the older workbook workflow and are described separately from the active runtime tables.

### movies

Canonical movie records.

```text
id uuid primary key
douban_subject_id text unique
douban_url text
title text not null
aka_titles jsonb
year integer
directors jsonb
actors jsonb
genres jsonb
countries jsonb
douban_rating numeric
douban_vote_count integer
summary text
poster_url text
raw_douban_json jsonb
metadata_status text
created_at timestamptz
updated_at timestamptz
```

### Legacy viewing_history_raw sketch

The current PostgreSQL repository does not create this table. Legacy raw imported rows were part of the workbook design; the current rebuild reads Google Sheets directly and local Excel files are historical snapshots only.

```text
id uuid primary key
source_sheet_name text
source_row_number integer
source_row_checksum text
date_raw text
name_raw text
director_raw text
year_raw text
rating_raw text
quality_raw text
comment_raw text
imported_at timestamptz
```

### viewing_history

Cleaned watched records.

```text
id uuid primary key
douban_subject_id text not null
movie_id uuid references movies(id) -- nullable backfill cache
watched_date date
user_rating numeric not null
quality text
comment text
source_sheet_name text
source_row_number integer
source_row_checksum text
deleted_at timestamptz nullable
created_at timestamptz
updated_at timestamptz
index(source_sheet_name, source_row_number) -- non-unique cached locator
```

`id` is the stable viewing-event identity. `source_sheet_name + source_row_number` is a mutable cached Sheet locator and must be validated against the row's hidden `RecordId` before update or delete. `source_row_checksum` covers A:I only and verifies projected content. `douban_subject_id` identifies the movie, not the viewing event; repeated viewings remain separate UUID rows.

### sheet_sync_outbox

One replacing pending task per history UUID:

```text
history_id uuid primary key references viewing_history(id)
operation text check (operation in ('upsert', 'delete'))
attempts integer
last_error text nullable
updated_at timestamptz
```

History mutation and outbox replacement share one database transaction. The in-process worker processes tasks in deterministic order, updates the cached locator after a successful upsert, and removes only the exact task version it processed so a concurrent newer edit cannot be lost. Failures increment attempts and remain retryable. Deletes are local soft deletes first and exact-UUID Sheet row deletions later. After a delete, an existing candidate-pool row is reactivated only when no other active `viewing_history` row and no active wishlist row exists for that movie. Current effective `not_interested` feedback remains an independent hard exclusion in recommendation filtering.

The Sheet projection occupies A:J. A:I retains the existing user fields; J is the hidden `RecordId`. Changing `watched_date` across years writes the target year tab before deleting the old row. Runtime sync is one-way, so direct edits to managed Sheet rows are unsupported.

### Migration And Recovery

Run and review the read-only reconciliation first:

```bash
.venv/bin/python -m jobs.migrate_viewing_history_record_ids audit --config-path .env
```

Apply the idempotent schema migration, then pass the reviewed audit report explicitly to the resumable `RecordId` backfill:

```bash
.venv/bin/python -m jobs.migrate_viewing_history_schema apply --config-path .env
.venv/bin/python -m jobs.migrate_viewing_history_record_ids apply \
  --config-path .env \
  --audit-report data/audits/viewing-history-record-id-audit-YYYYMMDD-HHMMSS.json
```

Progress is stored in `data/cache/viewing-history-record-id-migration.json`; rerunning the same apply command is safe. Timestamped audit and apply reports remain under `data/audits/`. On interruption, rerun apply and then audit again. Do not remove `RecordId` or restore the old unique locator index after the application has begun writing through the outbox; recovery is to leave pending tasks intact, correct the external failure, and retry from the History UI or backend startup.

Inspect sync health with `GET /viewing-history-sync/status`. Retry one failed task with `POST /viewing-history/{history_id}/sync`.

### Legacy matching data

The workbook importer keeps resumable match and review state in `data/cache/import-auto-match-progress.json`. `douban_match_candidates` below is a historical design sketch, not a table created by the current PostgreSQL repository.

```text
id uuid primary key
raw_history_id uuid references viewing_history_raw(id)
candidate_subject_id text
candidate_url text
candidate_title text
candidate_year integer
candidate_directors jsonb
candidate_rating numeric
match_score numeric
match_reasons jsonb
status text
review_note text
created_at timestamptz
updated_at timestamptz
```

Suggested statuses:

```text
auto_matched
needs_review
confirmed
rejected
no_match
```

### candidate_pool

Movies eligible for recommendation.

```text
id uuid primary key
movie_id uuid references movies(id)
source_type text
source_ref text
source_label text
active boolean
created_at timestamptz
updated_at timestamptz
```

When a movie is recorded as watched from a recommendation card, its
candidate-pool entry should be marked inactive after the viewing-history write
succeeds. Viewing history remains the source of truth for watched state; the
inactive flag prevents the candidate row from continuing to behave as an active
recommendation source.

This applies to every watched-recording path, including movies recorded from
wishlist. Once a movie is in viewing history, any active candidate-pool rows for
that movie should be inactive.

Adding a movie to wishlist should not mark candidate-pool rows inactive. Active
wishlist state is the exclusion mechanism. If the movie is later removed from
wishlist and downgraded to maybe-later, the candidate-pool row can remain active
unless another state such as watched or current not-interested makes it
ineligible.

Marking a movie as not interested should append a `not_interested` feedback
event and mark active candidate-pool rows inactive. Clearing not-interested can
restore candidate-pool rows only when the movie is otherwise eligible.

Marking a movie as maybe-later should not deactivate candidate-pool rows in this
frontend slice. It is a weak signal and may later feed recency/downrank logic.

The record-watched request carries the originating recommendation item when the
user starts from a recommendation card. It saves `viewing_history` and its sync
task first, then separately marks the recommendation item watched and
deactivates the candidate-pool row. If this later step fails, the API returns
the saved history with a warning. Recommendation processing status belongs to
`recommendation_items`, not `viewing_history`.

### candidate_subject_queue

Douban subject IDs discovered before metadata enrichment. This queue is the
resumable boundary between discovery and detail-page fetching.

```text
douban_subject_id text primary key
source_type text
source_ref text
source_subject_id text
source_label text
status text
error text
created_at timestamptz
updated_at timestamptz
```

`history_recommendation_discovery` tracks watched subjects already scanned for one-layer recommendations, so discovery can resume without repeating completed work.

Initial discovery writes Top250 subjects with:

```text
source_type = douban_top250
source_ref = top{rank}
```

Candidate queue processing is intentionally one layer deep for the first
version:

```text
Top250 subject
-> enrich movie detail
-> add movie to candidate_pool
-> discover "喜欢这部电影的人也喜欢" subject IDs
-> enqueue recommended subjects with source_ref = recommended_from:{subject_id}
```

Recommended subjects are queued for later enrichment but their own
recommendations are not expanded in the first version.

When a queued recommendation is activated into `candidate_pool`, the source
movie title should be preserved as `source_label`. Recommendation API responses
should expose this as a UI-ready label. Top-list sources may display raw
`source_ref` values such as `top17`; recommendation-derived sources should
display labels such as `Recommend from Yi Yi`, not raw Douban subject IDs.

### recommendation_sessions

One click-generated batch.

```text
id uuid primary key
strategy text
created_at timestamptz
context_snapshot jsonb
```

### recommendation_items

Movies returned in a session.

```text
id uuid primary key
session_id uuid references recommendation_sessions(id)
movie_id uuid references movies(id)
rank integer
slot_type text
score numeric
score_components jsonb
source_ref text
source_label text
processing_status text
processed_at timestamptz
created_at timestamptz
```

Suggested slot types:

```text
exploit
explore
```

### feedback

User feedback on recommendations.

```text
id uuid primary key
session_id uuid references recommendation_sessions(id)
item_id uuid references recommendation_items(id)
movie_id uuid references movies(id)
feedback_type text
feedback_value numeric
comment text
created_at timestamptz
```

Current feedback types:

```text
want_to_watch
maybe_later
not_interested
opened_douban
removed_from_wishlist
clear_not_interested
```

Feedback normally records user-signal events. Undoing a processed recommendation
can delete its latest matching feedback event. Removing a movie from wishlist appends a
`removed_from_wishlist` event that downgrades the current state to maybe-later
semantics. Removing a movie from the not-interested view should append
`clear_not_interested`. Recommendation filtering should derive current movie
state from the latest relevant event, not from the existence of any historical
negative event alone.

Clearing not-interested may reactivate the movie in `candidate_pool`, but only
when it is still otherwise eligible: not present in `viewing_history` and not in
active wishlist.

The not-interested list should show only the current effective
`not_interested` state. Movies with historical `not_interested` feedback that
were later cleared by `clear_not_interested` should not appear in the list.

### wishlist

Current and historical want-to-watch state.

```text
id uuid primary key
movie_id uuid references movies(id)
source_session_id uuid references recommendation_sessions(id)
status text
created_at timestamptz
closed_at timestamptz
```

Suggested statuses:

```text
active
watched
removed
```

## Recommendation Strategy

### Candidate Filtering

Before scoring:

1. start from active candidate_pool movies
2. exclude viewing_history movies
3. exclude active wishlist movies
4. exclude hard negative feedback when appropriate
5. optionally downrank recent maybe-later movies

### Popularity Baseline

Ranks by public quality:

```text
score = douban_rating * 0.75 + log10(max(douban_vote_count, 1)) * 0.25
```

Use vote count to avoid overrating obscure movies with tiny sample sizes.

### Content-Based Baseline

Build a user preference profile from watched movies:

- strong positive: rating >= 4.5
- positive: 4.0 <= rating < 4.5
- negative: rating < 4.0

Feature groups:

- genres
- countries
- directors
- actors
- decade/year

Score candidates by similarity to the positive profile minus similarity to the negative profile.

Current implementation in `backend/app/recommenders/simple.py`:

`content_score` is the personal-preference part of the baseline recommender. It asks:

```text
How much does this candidate look like movies the user rated highly,
minus how much it looks like movies the user rated poorly?
```

It builds two feature profiles from `viewing_history` once per recommendation run:

- `positive_profile`: features from watched movies with rating >= 4.0
- `negative_profile`: features from watched movies with rating < 4.0

Each movie contributes simple metadata features:

```text
features(movie) =
  genres
  countries
  directors
  first three actors
  decade
```

Rating controls how strongly watched movie features are added:

```text
positive_profile:
  rating >= 4.5        -> each feature weight +1.0
  4.0 <= rating < 4.5  -> each feature weight +0.6

negative_profile:
  rating < 4.0         -> each feature weight +1.0
```

For a candidate, the score sums the candidate's matching positive weights, subtracts matching negative weights, then divides by the candidate's own feature count:

```text
content_score(candidate) =
  (sum positive weights for candidate features
   - sum negative weights for candidate features)
  / max(candidate feature count, 1)
```

A candidate scores higher when its genres, countries, directors, top actors, or decade appear often in highly rated viewing history. It scores lower when those same kinds of features appear in low-rated viewing history.

### Hybrid Recommender

Recommended first production strategy:

```text
hybrid_score =
  personal_preference_score
  + public_quality_score
  + recent_interest_score
  + novelty_score
  + diversity_adjustment
  - repetition_penalty
  - negative_feedback_penalty
```

The returned batch should contain:

- four exploit items with high hybrid_score
- four explore items selected to increase diversity across genre, country, era, director, or popularity level

Current simple baseline implementation:

```text
public_quality = douban_rating * 0.75 + log10(max(douban_vote_count, 1)) * 0.25
personal_preference = content_score(candidate)
novelty = 0.3 if douban_vote_count < 100000 else 0.0

hybrid_total =
  personal_preference * 0.45
  + public_quality * 0.45
  + novelty * 0.10
```

Frontend score display normalizes the raw `hybrid_total` to a 100-point display
score. The current display baseline is `你的名字。 君の名は。`, whose measured
`hybrid_total` is approximately `23.4568` on the current local dataset:

```text
display_score = round(hybrid_total / 23.4568 * 100)
```

This normalization is display-only. Ranking, persistence, and explore sampling
continue to use raw `hybrid_total`.

The service first scores every eligible candidate, sorts by `hybrid_total`, and assigns:

```text
exploit slots = top 4 by hybrid_total
```

Then it selects four explore slots from the remaining candidates. Explore selection
first ranks remaining candidates by:

```text
explore_rank_score = diversity_gain * 0.65 + hybrid_total * 0.35
```

Each explore slot samples from the top-ranked explore pool with weights derived
from `explore_rank_score`. This keeps explore candidates quality-bounded while
allowing repeated recommendation sessions to vary. Passing an explore seed makes
the sampling reproducible for API checks and evaluation runs.

`diversity_gain` compares the candidate against movies already selected in the current batch:

```text
genre_gain =
  count(candidate genres not already selected)
  / max(candidate genre count, 1)

country_gain =
  count(candidate countries not already selected)
  / max(candidate country count, 1)

decade_gain =
  1.0 if candidate decade has not appeared in selected movies
  else 0.0

diversity_gain =
  genre_gain * 0.45
  + country_gain * 0.35
  + decade_gain * 0.20
```

This diversity score is batch-local. It prevents a single recommendation session from returning eight very similar movies; it does not measure novelty against the user's full viewing history.

### Contextual Bandit Strategy

`bandit_hybrid` is implemented in `backend/app/recommenders/bandit.py` and
`backend/app/services/recommendation_service.py`. It keeps the first four exploit
slots on the hybrid ranker and uses diagonal Linear Thompson Sampling for the
four explore slots after at least 20 trainable examples. With fewer examples or
on a training error, it falls back to hybrid diversity exploration.

The API and fresh frontend default remain `hybrid`. The frontend header now
offers a strategy selector and persists the selection in local storage. Both
strategies preserve hard exclusions, exposure cooldown, maybe-later
downranking, and batch-level diversity.

See `docs/contextual-bandit-design.md` for the detailed algorithm, feature
versioning, reward mapping, persistence rules, and fallback behavior. See
`docs/checklists/contextual-bandit-implementation-checklist.md` for the
completed backend implementation record.

### Feedback Weights

Bandit training rewards (resolved from feedback and watched ratings, rather than the stored `feedback_value` numbers):

```text
want_to_watch: +0.10
maybe_later: +0.05
not_interested: -1.00
opened_douban: logged, not trained in bandit v1
watched rating < 4.0: -1.00
watched rating 4.0 to 5.0: rating - 4.0
```

The stored `feedback_value` field uses a separate immediate-feedback scale. The bandit rewards above are training-time interpretations and can be tuned after real use.
See `docs/contextual-bandit-design.md` for bandit-specific reward resolution and training exclusions.

## Current FastAPI Endpoints

```text
GET  /movies/search?q={query}
GET  /candidate-queue/status
POST /candidate-queue/process
POST /viewing-history
GET  /viewing-history?year={year}&limit={limit}&offset={offset}&order={asc|desc}
PATCH /viewing-history/{history_id}
DELETE /viewing-history/{history_id}
POST /viewing-history/{history_id}/sync
GET  /viewing-history-sync/status
GET  /recommendations?strategy=hybrid
GET  /recommendations?strategy=bandit_hybrid
GET  /recommendations/{session_id}
POST /recommendations/{session_id}/items/{item_id}/feedback
DELETE /recommendations/{session_id}/items/{item_id}/processing

GET  /wishlist
POST /wishlist/{wishlist_id}/watched
DELETE /wishlist/{wishlist_id}

GET  /not-interested
DELETE /not-interested/{movie_id}
```

Import, rebuild, bulk enrichment, and evaluation operations are CLI jobs under `jobs/`. The desktop candidate-queue control endpoints process queued subjects in the background.

## Frontend Views

### Recommendations

Primary screen.

- button to request eight recommendations
- selector for `hybrid` or `bandit_hybrid`, with `hybrid` as the fresh-install default
- eight movie cards
- actions:
  - want-to-watch
  - maybe-later
  - not-interested
  - open Douban
- card fields:
  - title
  - year
  - director
  - main cast
  - Douban rating
  - Douban URL

### Wishlist

- active wishlist movies
- open Douban URL
- mark watched
- remove from wishlist

### Add Watched

- title, subject ID, or Douban URL search
- watched date
- rating
- quality
- comment
- source-tab return after success

### Not Interested

- current not-interested movies
- clear current negative-interest state

## Import And Enrichment Reliability

Douban ingestion should be implemented as a fragile adapter:

- browser automation is allowed for import jobs
- use low request rate
- cache raw page payloads
- persist intermediate state
- resume failed jobs
- keep manual review path for uncertain matches
- do not call Douban during live recommendation

## Why RL Is Deferred

The system has one user, sparse real-time feedback, and slow post-watch rewards. A true RL agent would be hard to train meaningfully in the first version. The MVP should log feedback in a reward-like structure so later approaches can be added:

- contextual bandit
- learning-to-rank
- RL-style sequential recommender

The first product version should optimize for a usable recommendation loop, reliable data ingestion, and observable baseline comparisons.
