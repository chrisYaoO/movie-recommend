# Context

## Product Definition

### Personal Movie Recommendation System

A single-user movie recommendation system for helping the user choose what to watch. The product's primary job is to produce useful on-demand recommendations from the user's own viewing history, enriched movie metadata, and explicit feedback.

Course-style reinforcement learning recommendation projects are only references. They may inspire later reward logging, exploration, or sequential-policy design, but they do not define the delivery requirements.

### First-Version Goal

The first usable version should close the practical recommendation loop:

```text
import viewing history
-> match and enrich movies
-> build local candidate pool
-> click to recommend six movies
-> collect feedback
-> maintain wishlist
-> record watched wishlist movies back into viewing history
```

The first version should be useful before true RL exists.

## User Workflow

### Recommendation Session

A recommendation session is an on-demand interaction started by the user clicking a recommendation action. It is not a scheduled or daily feed.

Each session returns exactly six movie candidates:

- four high-confidence candidates
- two exploratory or diversity candidates

The mix should balance immediate preference fit with discovery so the system does not narrow too quickly.

### Movie Card

The main movie card should prioritize decision information, not model-debug explanation.

Required card information:

- title
- year
- director
- main cast
- Douban rating
- awards, when available
- clickable Douban URL

Model explanations can be added later or exposed in a debug view, but they are not required in the main MVP card.

### Wishlist Loop

Want-to-watch feedback adds the movie to a wishlist. Active wishlist movies should be excluded from future recommendation sessions, but their metadata should still contribute to a recent-interest profile.

The first version should include a direct entry path for recording a watched wishlist movie into viewing history with:

- watched date
- rating
- quality
- comment

After recording, the movie should leave the active wishlist and become part of viewing history. Integration with the user's existing review tool is a later enhancement.

## Feedback Semantics

### Feedback

Feedback is any user signal collected during or after recommendation. Pre-watch feedback is weaker than post-watch rating and comment.

Primary feedback actions:

- `want_to_watch`: strong positive signal; adds movie to wishlist
- `maybe_later`: low positive signal; does not add movie to wishlist
- `not_interested`: negative signal

Auxiliary actions:

- opening the Douban URL may be logged as a weak interest signal, but it must not equal positive preference by itself
- already-watched or match-error actions are data-quality corrections, not primary recommendation feedback

### Rating Semantics

The user's ratings usually range from 3.5 to 5 and should be interpreted as personal preference, not general movie quality.

- below 4.0: negative feedback for the user's taste; disappointing or bad
- 4.0 to 4.5: worth watching; medium-to-strong positive
- 4.5 and above: essential favorite; very strong positive
- missing rating: unknown, not negative

## Data Sources

### Viewing History Source

The user's historical viewing record is maintained in Google Sheets. Local Excel files are historical snapshots only and should be ignored for the next rebuild unless the user explicitly asks to use them.

Current Excel columns:

- Date
- Name
- Director
- Year
- Rating
- Quality
- Comment

`Name` and `Rating` are sufficient for minimal import. `Director` and `Year` help disambiguate movie identity during matching.

The stable source identity for a viewing-history row should be `source_sheet_name + source_row_number`. This identity should replace the previous source-row hash as the uniqueness contract for `viewing_history`. A row-content checksum can still be kept as an optional change-detection checksum, but it should not be the primary unique row identity.

The rebuild path is intentionally split. Google Sheets plus the confirmed progress JSON first rebuilds `viewing_history` only, storing `douban_subject_id` directly on each watched row. It must not fetch Douban detail pages during this step. `viewing_history.movie_id` is nullable and should be treated as a backfilled local cache after `movies` rows exist.

### Candidate Movie Source

The preferred candidate source is Douban. A practical ingestion path is:

```text
collect or discover Douban subject IDs
-> open subject pages with browser automation when needed
-> extract embedded application JSON or structured page data
-> store normalized metadata and raw payload locally
```

This should be treated as a fragile import/enrichment adapter, not a stable public API. Recommendation must read local data only and should not call Douban in real time.

Optional future fallback sources include TMDB, IMDb, or Wikidata.

### Candidate Pool

The first version should target a local candidate pool of roughly 1000 to 3000 unwatched movies.

Candidate sources should include:

- Douban high-score lists
- Douban yearly or category lists
- movies similar to the user's high-rated history
- diversity-filling pools for genre, country, and era coverage

Candidate collection runs as a low-speed background import job, not as part of real-time recommendation.

The first candidate ingestion path is split into discovery and enrichment:

```text
discover Douban Top250 subject IDs
-> queue with source_type=douban_top250 and source_ref=top{rank}
-> process queue with resumable status
-> enrich missing movie details into movies
-> activate candidates in candidate_pool
-> enqueue one-layer "recommended from {movie}" subject IDs
```

The first version expands only `Top250 -> one layer of recommendations`.
Recommended subjects are queued and enriched, but their own recommendations are
not recursively expanded.

During movie-detail enrichment, the parser should also capture Douban "recommended from this subject" links and enqueue them into `candidate_subject_queue`. This applies both when rebuilding details for watched history movies and when processing Top250 candidates.

## Persistence

### PostgreSQL As System Of Record

Matched and enriched viewing history should be stored in PostgreSQL so the recommender, feedback loop, baseline evaluation, and frontend all read a consistent local database.

PostgreSQL should store:

- raw Excel rows
- Douban match candidates and match decisions
- canonical movies and external IDs
- enriched movie metadata
- viewing history
- candidate pool entries
- candidate subject queue entries
- recommendation sessions and returned items
- feedback events
- wishlist state

For the next database rebuild, `viewing_history` should be reconstructed from Google Sheets plus the existing auto-match progress JSON by matching sheet name and `source_row_number`. During the rebuild, all database tables except the two candidate tables, `candidate_subject_queue` and `candidate_pool`, may be cleared after explicit user approval. The `movies` table should then be reloaded from Douban subject detail pages referenced by viewing history, then expanded through candidate discovery. The `movies` schema should drop `display_title` and `original_title`; other metadata columns should remain unless the user approves a further schema change.

When replaying the progress JSON, confirmed subject IDs should be selected by explicit priority: `manual_id_persisted` over `review_confirmed_persisted` over `auto_matched_persisted`. If the highest-priority confirmed entries for one source row disagree on subject ID, the row should be reported and skipped rather than guessed.

After `viewing_history` is rebuilt, the movie rebuild job should read distinct `viewing_history.douban_subject_id` values that are missing from `movies`, fetch Douban detail pages, upsert canonical metadata into `movies`, backfill `viewing_history.movie_id`, and enqueue one-layer Douban recommendations into `candidate_subject_queue`.

For incremental record-watched updates, the request should append to Google Sheets and upsert `viewing_history`. If `movies` already contains the watched subject, `movie_id` can be filled immediately. If not, the request should synchronously fetch that watched movie's Douban detail, write `movies`, and fill `viewing_history.movie_id`. Detail-page recommendations may be inserted into `candidate_subject_queue`, but recommended subjects should be turned into `movies` plus `candidate_pool` entries by the background queue processor, not by the synchronous record-watched request.

## Recommendation Strategy

### First-Version Strategy

The first version should not depend on a true RL agent. It should implement strong baselines and a hybrid recommender first, while logging feedback in a way that can support later contextual bandit, learning-to-rank, or RL-style sequential policies.

Initial strategies:

- popularity baseline
- content-based similarity baseline
- hybrid ranker

The normal frontend should default to the hybrid strategy. The backend should allow strategy switching for evaluation and debugging.

### Hybrid Ranking Signals

The hybrid ranker should combine:

- personal preference from viewing history
- public quality from Douban rating and vote count
- recent interest from wishlist and maybe-later feedback
- diversity across genre, country, era, director, and popularity level
- novelty
- repetition penalties
- negative feedback penalties

Wishlist is a medium-strength recent positive signal. Maybe-later is weaker positive signal. Not-interested is negative feedback.

## Technical Stack

Accepted stack:

- backend API: Python FastAPI
- database: PostgreSQL
- data jobs: Python with pandas and Selenium or Playwright
- recommendation libraries: Python ML ecosystem, starting with simple scikit-learn style methods
- frontend: React with TypeScript

## MVP Boundary

### Included

- Excel import
- Douban matching and metadata enrichment
- PostgreSQL persistence
- local candidate pool
- on-demand API endpoint returning six recommendations
- thin React frontend with six movie cards
- want-to-watch, maybe-later, and not-interested feedback
- wishlist
- direct record-watched entry from wishlist
- backend strategy switching for popularity, content-based, and hybrid recommendation

### Excluded

- true RL agent
- daily automatic recommendations
- multi-user support
- full Douban crawling
- production deployment
- mobile-first polish
- complex analytics dashboard
- LLM-generated recommendation explanations
- integration with the user's existing review tool

## Related Docs

- `docs/requirements.md`: functional and non-functional requirements
- `docs/architecture.md`: data flow, schema draft, modules, endpoints, and recommendation mechanics
- `docs/agents/`: local agent workflow configuration
