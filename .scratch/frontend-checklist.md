# Frontend Checklist

## Purpose

Plan the next frontend iteration for the existing thin React app. This document is ordered by implementation dependency:

1. Backend/API contracts that the frontend depends on.
2. Shared frontend state and card components.
3. Recommend, Add watched, Wishlist, and Not interested views.
4. Later targets that should not block this slice.

## Execution Rules

- Complete one small checklist section at a time, then report back and wait for user confirmation.
  - Only after confirmation, update checklist item status and proceed to the next section.
- If implementation reveals a design or architecture issue, or the current code cannot support the requested behavior without changing product requirements, stop and ask the user.
  - Do not make unilateral requirement or architecture decisions during implementation.

## Confirmed Product Decisions

- [x] Recommendation sessions return 6 cards.
  - Mix: 4 exploit cards and 2 explore cards.
- [x] Normal frontend strategy is fixed to `hybrid`.
  - Do not expose strategy controls in the primary UI.
- [x] Recommendation score is displayed as a 100-point normalized UI score.
  - Raw backend ranking continues to use `item.score`.
  - Baseline raw `hybrid_total`: `23.4568`, measured from the current best-fit baseline movie `Your Name`.
  - UI formula: `normalized_score = round(item.score / 23.4568 * 100)`.
- [x] Recommendation source label is user-readable.
  - If `source_ref` starts with `top`, display that value directly, such as `top17`.
  - If `source_ref` is `recommended_from:{subject_id}`, display `Recommend from {movie title}`.
  - Do not show raw Douban subject IDs in normal card UI.
- [x] `Not interested` is a main app tab beside Wishlist.
- [x] Wishlist tab lists only active wishlist items.
  - Do not show removed or watched wishlist rows.
- [x] Not interested tab lists only current effective `not_interested` movies.
  - Do not show historical `not_interested` movies that were later cleared.
- [x] Wishlist and Not interested lists sort by current-state time descending for now.
  - Filtering is a later target.
- [x] Tabs and page state should restore after closing/reloading the frontend.

## Backend/API Phase

### Recommendation Sessions

- [ ] Return 6 recommendation items: 4 exploit and 2 explore.
- [ ] Add/update tests for the 4 exploit plus 2 explore mix.
- [ ] Add persisted processing state to `recommendation_items`.
  - Suggested statuses: `watched`, `added_to_wishlist`, `not_interested`, `maybe_later`.
  - Add `processing_status` and `processed_at`.
  - Include processing status in recommendation API responses.
- [ ] Add an API path to fetch an existing recommendation session by id.
  - Suggested endpoint: `GET /recommendations/{session_id}`.
  - Used by frontend reload restoration to sync the same session without generating new recommendations.
- [ ] Expose recommendation item source label.
  - Required UI field: `source_label`.
  - `top{rank}` can be returned/displayed directly.
  - `recommended_from:{subject_id}` must be resolved to `Recommend from {movie title}`.
  - Raw `source_ref` may be exposed only as optional debug data.

### Feedback And Candidate State

- [ ] Treat feedback rows as append-only user-signal events.
  - Do not mutate/delete old feedback events to represent later state changes.
- [ ] Add feedback state-change types.
  - `removed_from_wishlist`: active wishlist item was removed and downgraded to maybe-later semantics.
  - `clear_not_interested`: current not-interested state was cleared.
- [ ] Derive current effective movie state from the latest relevant feedback/state event.
  - Do not exclude a movie only because it has any historical `not_interested` event.
- [ ] `want_to_watch` behavior:
  - Add or keep active wishlist row.
  - Mark originating `recommendation_items.processing_status = added_to_wishlist`.
  - Do not deactivate candidate-pool rows; active wishlist state is the exclusion mechanism.
- [ ] `not_interested` behavior:
  - Append `not_interested` feedback.
  - Mark originating `recommendation_items.processing_status = not_interested`.
  - Mark active candidate-pool rows for the movie inactive.
- [ ] `maybe_later` behavior:
  - Append `maybe_later` feedback.
  - Mark originating `recommendation_items.processing_status = maybe_later`.
  - Do not deactivate candidate-pool rows in this slice.
- [ ] `clear_not_interested` behavior:
  - Append `clear_not_interested`.
  - Restore `candidate_pool.active=true` only if the movie is not watched and not in active wishlist.

### Watched Recording

- [ ] Any successful watched-history write should make active candidate-pool rows for that movie inactive.
- [ ] From a recommendation card:
  - Add watched submission carries `session_id` and `recommendation_item_id`.
  - Backend writes viewing history.
  - Backend marks originating `recommendation_items.processing_status = watched`.
  - Backend marks active candidate-pool rows for the movie inactive.
  - Response includes enough information for the frontend to mark the originating card processed.
  - Do not store recommendation processing status on `viewing_history`.
- [ ] From a wishlist card:
  - Backend writes viewing history.
  - Backend closes/removes the active wishlist item.
  - Backend marks active candidate-pool rows for the movie inactive.

### Wishlist API

- [ ] `GET /wishlist` returns only active wishlist items.
  - Sort by current-state time descending.
  - Support pagination, 10 rows per page.
- [ ] Add wishlist remove endpoint if missing.
  - Semantics: close/remove active wishlist item.
  - Append `removed_from_wishlist` state-change feedback.
  - Do not mark as `not_interested`.

### Not Interested API

- [ ] Add not-interested list endpoint.
  - Return only movies whose current effective state is `not_interested`.
  - Sort by current-state time descending.
  - Support pagination, 10 rows per page.
- [ ] Add not-interested remove endpoint.
  - Semantics: append `clear_not_interested`.
  - Do not mutate/delete the original `not_interested` event.
  - Restore candidate-pool active state only when otherwise eligible.

### Search/Add Watched API

- [ ] Keep movie search API repair as a later backend/API target.
- [ ] Support direct Douban subject ID input/resolution as a later target.

## Frontend Phase

### App State

- [ ] Persist active tab in localStorage.
- [ ] Preserve tab state when switching tabs.
- [ ] Restore page state after closing/reloading the frontend.
- [ ] Recommend reload behavior:
  - If a cached current recommendation session exists, render it first.
  - Background-sync the same session from backend using `GET /recommendations/{session_id}`.
  - Do not generate a new recommendation session during restoration.
  - If no cached session exists, load hybrid recommendations with `seed=42`.
- [ ] Refresh behavior:
  - Refresh button loads a new recommendation session.
  - Current debug seed: `24`.
  - Future behavior: random seed.
  - Refresh success replaces the current recommendation session cache.
  - Older backend sessions remain historical but are not restored as the current UI session.
- [ ] Wishlist and Not interested reload behavior:
  - Render local cached list first.
  - Background-sync the first page or current visible range.

### Shared Movie Card

- [ ] Reuse one shared card across Recommend, Wishlist, and Not interested views.
- [ ] Top-left badge shows slot/source tag, not rank.
  - Recommendation cards: `Explore` or `Exploit`.
- [ ] Top-right shows Douban rating.
- [ ] Reserve a stable poster/image area above the title.
  - Placeholder only in this slice.
  - Real poster loading is a later target.
- [ ] Movie title links to Douban URL.
- [ ] Remove the separate Douban link row.
- [ ] Keep year, director, and cast visible for now.
  - Director/cast formatting is a later target.
- [ ] Under cast, show left-side score: `Score: {normalized_score}`.
- [ ] To the right of score, show source label.
  - Top sources: `top{rank}`.
  - Recommendation-derived sources: `Recommend from {movie title}`.
- [ ] Hide button row by default.
- [ ] Show button row on card hover/focus.
- [ ] Processed cards stay visible.
  - Use muted/greyed styling or lower opacity.
  - Show status text: `Watched`, `Added to wishlist`, `Not interested`, or `Maybe later`.
  - Disable/hide actions that no longer apply.
  - Do not auto-refresh recommendations because a card was processed.

### Recommend View

- [ ] Remove top title/introduction block:
  - `Movie Recommender`
  - `Local recommendations and viewing history.`
- [ ] Remove manual seed input.
- [ ] Remove manual "Recommend" flow.
- [ ] Show 6 recommendation cards.
- [ ] Add Refresh button at the recommendation panel top-right.
- [ ] Recommendation button row labels:
  - `Watched`
  - `+`
  - `-`
  - `Later`
- [ ] Recommendation button behavior:
  - `Watched`: switch to Add watched, preselect movie, preserve originating `session_id` and `recommendation_item_id`.
  - `+`: submit `want_to_watch`, mark card `Added to wishlist`.
  - `-`: submit `not_interested`, mark card `Not interested`.
  - `Later`: submit `maybe_later`, mark card `Maybe later`.

### Add Watched View And Form

- [ ] When opened from Recommend or Wishlist, preselect the movie.
- [ ] Keep search area visible and usable after preselection.
  - User can search again and choose a different movie if the handoff was wrong.
- [ ] Remove the visible `Movie` label text from the search toolbar.
- [ ] Change `Quality` from free text to options:
  - `1080p`
  - `4K`
  - `Other`
- [ ] When `Other` is selected, show a custom quality input.
  - Submit only the custom text.
  - Do not prefix the submitted value with `Other`.
- [ ] Remove the visible `Sheet` input.
- [ ] Derive submitted `sheet` from watched date year.
  - Example: `2026-05-28` submits `sheet=2026`.
- [ ] Restrict rating to `0` through `5`.
- [ ] Rating step is `0.1`.
- [ ] Preserve draft on submit failure, tab switch, close, or reload.
  - Use localStorage-backed draft state.
  - Persist selected movie with form fields.
  - Selected movie draft fields: `subject_id`, `title`, `year`, `director`, `url`.
- [ ] On successful submit:
  - Clear localStorage draft.
  - Reset selected movie, query, candidates, and form values to defaults.
  - If opened from another tab, return to that source tab.
  - If opened directly from Add watched, stay on Add watched.

### Wishlist View

- [ ] Reuse shared movie card.
- [ ] Show only active wishlist items.
- [ ] Move Refresh button to the top-right.
- [ ] Remove current standalone wishlist card markup.
- [ ] Button labels:
  - `Watched`
  - `Remove`
- [ ] `Watched` switches to Add watched and preselects the wishlist movie.
- [ ] `Remove` closes/removes active wishlist item and records `removed_from_wishlist`.
- [ ] Load at most 10 rows at a time.
- [ ] Load next page when scrolling near the bottom.

### Not Interested View

- [ ] Add `Not interested` tab.
- [ ] Show only current effective not-interested movies.
- [ ] Reuse shared movie card.
- [ ] Show only one action: remove from not interested.
- [ ] Remove action appends `clear_not_interested`.
- [ ] Load at most 10 rows at a time.
- [ ] Load next page when scrolling near the bottom.

## Later Targets

- [ ] Randomize Refresh seed after debug phase.
- [ ] Load real poster images.
- [ ] Improve director and cast formatting.
- [ ] Repair movie search API.
- [ ] Support direct Douban subject ID input/resolution.
- [ ] Add maybe-later recency/downrank rules so repeatedly deferred movies are less likely to reappear soon.
- [ ] Add Wishlist and Not interested filters.

## Verification

- [ ] Run focused backend tests for changed recommendation/API behavior.
- [ ] Run frontend build.
- [ ] Browser-check:
  - Recommend initial load with no cache uses `seed=42`.
  - Recommend restore with cache does not generate a new session.
  - Refresh replaces current session cache.
  - Processed recommendation cards stay muted after reload.
  - Add watched handoff works from Recommend and Wishlist.
  - Add watched draft survives reload and clears after success.
  - Wishlist shows active items only and paginates by 10.
  - Not interested shows current effective negative items only and paginates by 10.
