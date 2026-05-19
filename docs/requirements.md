# Personal Movie Recommendation System Requirements

## Goal

Build a personal movie recommendation system that helps the user choose films on demand. The system is driven by the user's historical viewing record, enriched movie metadata, and explicit feedback collected from recommendation sessions.

Course-style reinforcement learning recommendation projects are references only. This product prioritizes the user's actual workflow over satisfying course-project constraints.

## Core Workflow

1. Import the user's existing Excel viewing history.
2. Match imported movies to Douban movie subjects.
3. Enrich matched movies and candidate movies with metadata.
4. Store cleaned history, candidates, feedback, and wishlist state in PostgreSQL.
5. User clicks a recommendation action in the frontend.
6. System returns exactly five unseen and non-wishlist movies.
7. User marks each recommendation as want-to-watch, maybe-later, or not-interested.
8. Want-to-watch movies enter the wishlist.
9. When a wishlist movie is watched, the user records rating, quality, comment, and watched date.
10. Watched wishlist movies move into viewing history and become strong feedback.

## MVP Functional Requirements

### Data Import

- Import an Excel viewing history file with these columns:
  - Date
  - Name
  - Director
  - Year
  - Rating
  - Quality
  - Comment
- Preserve raw imported rows for auditability.
- Generate stable row hashes so repeated imports do not duplicate records.
- Treat Excel as an import source, not the long-term system of record.

### Douban Matching

- Match imported rows to Douban subjects using Name, Year, and Director.
- Use automated search and scoring where possible.
- Automatically accept only high-confidence matches.
- Write ambiguous or failed matches to a review queue.
- Store match status, score, candidate subject IDs, and review decision.

### Metadata Enrichment

- Use Douban as the preferred metadata source.
- Support subject-page ingestion through browser automation and embedded page JSON extraction.
- Store raw Douban JSON or raw extracted payloads for repeatability.
- Cache enriched metadata locally and never require real-time Douban access during recommendation.
- Treat awards as optional metadata. Missing awards must not block recommendation.
- Keep TMDB or IMDb as future fallback sources.

### Candidate Pool

- Maintain a local candidate pool of roughly 1000 to 3000 unwatched movies.
- Candidate sources should include:
  - Douban high-score lists
  - Douban yearly or category lists
  - movies similar to the user's high-rated history
  - diversity-filling pools for genre, country, and era coverage
- Candidate ingestion runs as a background/import job, not inside the live recommendation path.
- The first Top250 discovery stores `source_ref=top{rank}` instead of a separate rank field.
- Candidate queue processing must be resumable and should only expand `Top250 -> one layer of recommendations` in the first version.
- Movie user state must not be stored on `movies`; derive watched state from `viewing_history`, want-to-watch from `wishlist`, negative interest from `feedback`, and candidate eligibility from `candidate_pool`.

### Recommendation Session

- A session starts only when the user clicks the recommendation action.
- Each session returns exactly five movies.
- The mix should be:
  - three high-confidence candidates
  - two exploratory or diversity candidates
- Exclude movies already in viewing history.
- Exclude movies already in wishlist.
- Avoid repeating recently rejected or repeatedly deferred movies unless enough time or context has changed.

### Movie Card

Each recommendation card should show decision information, not model-debug details:

- title
- year
- director
- main cast
- Douban rating
- awards, when available
- clickable Douban URL

Recommendation explanations can be added later or exposed in a debug view, but they are not required for the main card in MVP.

### Feedback

Primary feedback actions:

- want-to-watch
  - strong positive signal
  - adds movie to wishlist
- maybe-later
  - low positive signal
  - does not add movie to wishlist
- not-interested
  - negative signal

Auxiliary actions:

- open Douban URL
  - may be logged as a weak interest signal
  - must not equal positive preference by itself
- mark already watched or matching error
  - data-quality correction path
  - not a primary workflow

### Wishlist

- Store want-to-watch movies in a wishlist.
- Exclude wishlist movies from future recommendation sessions.
- Use wishlist metadata as medium-strength recent preference signal.
- Provide a direct entry path to record a wishlist movie as watched.
- When recorded as watched:
  - remove or close the wishlist entry
  - create a viewing history record
  - store rating, quality, comment, and watched date
  - use the rating and comment as strong feedback

### Rating Semantics

The user's rating scale is selective and personal:

- below 4.0: negative feedback, disappointing or bad for the user's taste
- 4.0 to 4.5: worth watching, medium-to-strong positive
- 4.5 and above: essential favorite or very strong positive
- missing rating: unknown, not negative

### Baselines And Recommendation Strategies

The first version should include:

- popularity baseline
  - ranks by public quality signals such as Douban rating and vote count
- content-based baseline
  - ranks by similarity to the user's high-rated history
- hybrid recommender
  - combines personal preference, public rating, recency controls, diversity, novelty, and repetition penalties

The backend should support strategy switching for evaluation and debugging. The normal frontend should default to the hybrid strategy.

### Frontend

MVP frontend pages:

- recommendation page
  - click to request five recommendations
  - show five movie cards
  - capture want-to-watch, maybe-later, and not-interested feedback
- wishlist page
  - list saved movies
  - open Douban URL
  - record watched movie with rating, quality, comment, and date
- review queue page
  - resolve low-confidence Douban matches

### Backend API

Required API capabilities:

- import viewing history
- view import and match status
- resolve match candidates
- trigger metadata enrichment
- request recommendations by strategy
- submit recommendation feedback
- manage wishlist
- record watched movie

## Non-Functional Requirements

- Recommendation response should read local PostgreSQL data only.
- External web access must be limited to import and enrichment jobs.
- Import jobs should be resumable.
- Douban access should be low-speed and cached.
- The system should preserve raw external payloads for debugging.
- Data model should support future TMDB/IMDb fallback without rewriting core viewing history.
- The first version is single-user.
- The system should be usable locally.

## Explicit Non-Goals For MVP

- true RL agent
- daily automatic recommendations
- multi-user support
- full Douban crawling
- production deployment
- mobile-first UX
- complex analytics dashboard
- LLM-generated explanations
- integration with the user's existing review tool

## Later Enhancements

- integrate the user's existing review tool
- add TMDB/IMDb/Wikidata fallback enrichment
- add learning-to-rank or contextual bandit from accumulated feedback
- add RL-style sequential recommendation if enough interaction data exists
- add richer diversity controls
- add recommendation explanation/debug panel
- add evaluation reports comparing strategies over historical replay and live feedback
