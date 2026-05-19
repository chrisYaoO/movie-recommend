# Personal Movie Recommendation System Architecture

## System Shape

The system has four main parts:

1. data jobs
   - Excel import
   - Douban matching
   - Douban metadata enrichment
   - candidate pool construction
2. PostgreSQL
   - system of record for imported history, movies, feedback, wishlist, and recommendation sessions
3. FastAPI backend
   - recommendation API
   - feedback API
   - import/matching/admin API
4. React frontend
   - recommendation workflow
   - wishlist workflow
   - match review workflow

Live recommendation must not call Douban. It only reads PostgreSQL.

## Data Flow

```text
Excel viewing history
-> raw import table
-> Douban matcher
-> match candidates / confirmed matches
-> movie metadata enrichment
-> movies + viewing_history
-> recommender
-> recommendation session + recommendation items
-> frontend feedback
-> feedback + wishlist + viewing_history updates
```

Candidate ingestion follows a related path:

```text
Douban list/search/similar-source import
-> candidate subject IDs
-> Douban subject enrichment
-> movies
-> candidate_pool entries
```

## Module Layout

Suggested repository layout:

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
|   |   |-- schemas/
|   |   |-- services/
|   |   `-- recommenders/
|   |-- alembic/
|   `-- tests/
|-- jobs/
|   |-- import_excel.py
|   |-- match_douban.py
|   |-- enrich_douban.py
|   `-- build_candidate_pool.py
|-- frontend/
|   `-- src/
`-- data/
    |-- imports/
    `-- cache/
```

## PostgreSQL Schema Draft

### movies

Canonical movie records.

```text
id uuid primary key
douban_subject_id text unique
douban_url text
title text not null
original_title text
year integer
directors jsonb
actors jsonb
genres jsonb
countries jsonb
languages jsonb
runtime_minutes integer
douban_rating numeric
douban_vote_count integer
awards jsonb
summary text
poster_url text
raw_douban_json jsonb
metadata_status text
created_at timestamptz
updated_at timestamptz
```

### viewing_history_raw

Raw Excel rows.

```text
id uuid primary key
source_file text
source_row_number integer
source_row_hash text unique
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
movie_id uuid references movies(id)
source_raw_id uuid references viewing_history_raw(id)
watched_date date
user_rating numeric
quality text
comment text
created_at timestamptz
updated_at timestamptz
```

### douban_match_candidates

Search/matching audit table.

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
active boolean
created_at timestamptz
```

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
movie_id uuid references movies(id)
feedback_type text
feedback_value numeric
comment text
created_at timestamptz
```

Suggested feedback types:

```text
want_to_watch
maybe_later
not_interested
opened_douban
already_watched_correction
match_error
```

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
score = f(douban_rating, douban_vote_count)
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
- public rating bucket

Score candidates by similarity to the positive profile minus similarity to the negative profile.

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

- three exploit items with high hybrid_score
- two explore items selected to increase diversity across genre, country, era, director, or popularity level

### Feedback Weights

Initial weights:

```text
want_to_watch: +0.7
maybe_later: +0.2
not_interested: -0.8
opened_douban: +0.1
watched rating >= 4.5: +1.0
watched rating 4.0-4.49: +0.6
watched rating < 4.0: -1.0
```

These are product defaults, not fixed model truth. They should be tuned after real use.

## FastAPI Endpoint Draft

```text
POST /imports/viewing-history
GET  /imports/{import_id}/status

POST /douban/match/run
GET  /douban/matches?status=needs_review
POST /douban/matches/{candidate_id}/confirm
POST /douban/matches/{candidate_id}/reject

POST /douban/enrich/run
POST /candidate-pool/build

GET  /recommendations?strategy=hybrid
POST /recommendations/{session_id}/items/{item_id}/feedback

GET  /wishlist
POST /wishlist/{wishlist_id}/watched
DELETE /wishlist/{wishlist_id}

GET  /movies/{movie_id}
```

## Frontend Pages

### Recommendations

Primary screen.

- button to request five recommendations
- five movie cards
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
  - awards if present
  - Douban URL

### Wishlist

- active wishlist movies
- open Douban URL
- mark watched
- remove from wishlist

### Record Watched

Can be a modal from wishlist.

- watched date
- rating
- quality
- comment

### Match Review

Admin-style page.

- raw Excel row
- candidate matches
- match score and fields
- confirm / reject / no match

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
