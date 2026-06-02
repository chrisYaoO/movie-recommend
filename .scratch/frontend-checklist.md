# Frontend Checklist

## Purpose

Plan the next frontend iteration for the existing thin React app. This slice now includes the previous Later Targets plus the newly confirmed requirements:

1. Fix card metadata contracts: source labels, original wishlist score/source, posters, and person formatting.
2. Repair Add watched search, including direct Douban subject ID resolution.
3. Improve recommendation refresh, maybe-later recency/downrank behavior, and debug controls.
4. Add the requested UI polish: trash icons, Add watched single-column layout, Wishlist/Not interested filters.

## Execution Rules

- Complete one small checklist section at a time, then report back and wait for user confirmation.
  - Only after confirmation, update checklist item status and proceed to the next section.
- If implementation reveals a design or architecture issue, or the current code cannot support the requested behavior without changing product requirements, stop and ask the user.
  - Do not make unilateral requirement or architecture decisions during implementation.

## Confirmed Product Decisions

- [x] Recommendation sessions return 8 cards.
  - Mix: 4 exploit cards and 4 explore cards.
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
- [x] `Recommend from unknown movie` is a backend source-label resolution bug.
  - The frontend should not paper over normal recommendation-derived source labels.
  - Backend should resolve `recommended_from:{subject_id}` to the source movie title from local data whenever possible.
- [x] Wishlist card score/source shows the original recommendation context.
  - Use the score and source fields persisted on the originating `recommendation_items` row.
  - Do not recompute a current score for wishlist display.
  - If no originating recommendation item exists, return null score/source fields and keep the card renderable.
- [x] Movie cards should load real poster images when local metadata has `poster_url`.
  - Missing posters use the existing stable placeholder.
  - Poster loading is display-only; it must not call Douban live.
- [x] Director and cast formatting should be cleaned up for card readability.
  - Backend should return normalized arrays when available.
  - Frontend should render compact human-readable text without mojibake separators.
- [x] Search/Add watched should support both title search and direct Douban subject ID input.
  - Direct ID input should resolve to a selectable movie candidate.
  - If local metadata is missing, the watched-movie creation path may synchronously fetch the watched movie detail as already defined.
- [x] `Not interested` is a main app tab beside Wishlist.
- [x] Wishlist tab lists only active wishlist items.
  - Do not show removed or watched wishlist rows.
- [x] Not interested tab lists only current effective `not_interested` movies.
  - Do not show historical `not_interested` movies that were later cleared.
- [x] Wishlist and Not interested filters are now in this slice.
  - Filters should be lightweight client/API filters for finding items in longer lists, not a full analytics surface.
- [x] Trash icons replace only remove-style actions.
  - Wishlist trash icon keeps `removed_from_wishlist` semantics.
  - Not interested trash icon keeps `clear_not_interested` semantics.
  - Recommendation card `-` remains the `not_interested` feedback action for now.
  - Use an accessible label/title on icon-only buttons.
- [x] Add watched search uses a single-column flow above the record form.
  - Search panel appears above candidate results and the record form.
  - Desktop should not place search and form side-by-side.
  - Handoff preselection, draft persistence, and source-tab return behavior remain unchanged.
- [x] Formal Refresh is exposure-aware.
  - It creates a new recommendation session.
  - It omits a fixed explore seed.
  - It excludes movies exposed in recent recommendation sessions by default.
- [x] Debug recommendation mode has a narrow contract.
  - It disables poster loading.
  - It uses `exposure_cooldown_sessions=1`.
  - It uses fixed `seed=42` so explore slots are stable during manual debug refresh.
  - Debug controls may exist, but they should not clutter the normal recommendation workflow.
- [x] Maybe-later needs short-term recency/downrank behavior in this slice.
  - `maybe_later` remains a weak positive signal, not a hard negative.
  - Repeated or recent maybe-later items should be less likely to reappear soon.

## Current Slice Scope

The following items were previously listed as Later Targets and are now promoted into the current execution plan:

- [x] Load real poster images.
- [x] Improve director and cast formatting.
- [x] Repair movie search API.
- [x] Support direct Douban subject ID input/resolution.
- [x] Add maybe-later recency/downrank rules so repeatedly deferred movies are less likely to reappear soon.
- [x] Add Wishlist and Not interested filters.
- [x] Add a debug-only recommendation controls surface if repeated manual evaluation needs it.

## Recommended Execution Order

### Section 1: Backend Card Metadata Contract

Why first: Recommend, Wishlist, and Not interested cards all depend on one consistent movie-card response shape.

- [x] Fix `Recommend from unknown movie` at the data/API boundary.
  - `recommended_from:{subject_id}` should resolve to `Recommend from {source movie title}` when the source movie exists in `movies`.
  - Existing candidate rows with missing `source_label` should still render a title if the source movie can be found by subject ID.
  - Keep `Recommend from unknown movie` only as a last-resort debug fallback, not normal UI output.
- [x] Add a repair/backfill path for existing candidate source labels.
  - Backfill `candidate_pool.source_label` or queue-derived source labels from `movies.title`.
  - Do not require re-crawling Douban just to repair labels when the source movie already exists locally.
- [x] Expose poster and normalized person fields in movie-card API responses.
  - Include `poster_url` when stored on `movies`.
  - Return director/cast in a shape the frontend can render cleanly.
  - Preserve existing scalar fields during transition so current UI does not break.
- [x] Extend wishlist API responses with original recommendation display fields.
  - Required fields: `score`, `source_ref`, and `source_label`.
  - Source is the originating `recommendation_items` row matched by `wishlist.source_session_id + wishlist.movie_id`.
  - If no originating recommendation item can be found, return null values rather than inventing a score.
  - Preserve current active-wishlist filtering and pagination.
- [x] Add focused backend tests.
  - Top-list source returns `top{rank}`.
  - Recommendation-derived source with stored label returns `Recommend from {title}`.
  - Recommendation-derived source without stored label resolves through the source subject movie.
  - Missing source movie falls back safely without crashing.
  - Movie response includes poster/person fields when metadata exists.
  - Wishlist item created from a recommendation carries that recommendation's score and source label.
  - Wishlist item without recommendation context remains renderable.
- [x] Section verification:
  - Run focused backend tests for source-label, movie-card metadata, and wishlist response behavior.

### Section 2: Search And Direct Douban ID API

Why second: Add watched layout changes are only useful if search and direct ID resolution are reliable.

- [x] Repair movie search API behavior.
  - Search should return useful candidates for normal title input.
  - Search should not fail silently or return malformed candidate rows.
  - Response shape should stay compatible with the current Add watched candidate selector.
- [x] Support direct Douban subject ID input/resolution.
  - Detect subject IDs from plain IDs and Douban subject URLs.
  - Return an existing local movie when the subject ID already exists in `movies`.
  - If the movie does not exist locally, use the existing watched-movie detail creation path rather than adding a second enrichment path.
- [x] Add focused backend tests.
  - Title search returns candidate rows with `subject_id`, `title`, `year`, `director`, and `url`.
  - Plain Douban subject ID resolves.
  - Douban subject URL resolves.
  - Missing local metadata path returns a safe selectable candidate or a clear error that the frontend can show.
- [x] Section verification:
  - Run focused backend tests for movie search and direct ID resolution.

### Section 3: Backend Recommendation Controls

Why third: Refresh, debug reproducibility, and maybe-later downrank all affect recommendation serving semantics.

- [x] Add API support for session-based exposure cooldown.
  - Suggested endpoint shape: `GET /recommendations?strategy=hybrid&exposure_cooldown_sessions=5`.
  - The cooldown excludes movie IDs shown in the most recent N recommendation sessions.
  - Default production value: `5`.
  - Debug value: explicit, usually `1`.
  - Debug may also set `0` to inspect pure deterministic ranking.
- [x] Keep seed behavior explicit.
  - `seed` controls reproducible explore sampling.
  - Production Refresh does not send `seed`.
  - The current frontend debug mode sends fixed `seed=42`; explicit seed controls can be added later if manual reproducibility needs more than two-batch rotation.
- [x] Apply cooldown after hard eligibility filters.
  - Hard exclusions remain watched history, active wishlist, and current effective `not_interested`.
  - Exposure cooldown is a soft freshness filter, not permanent negative feedback.
- [x] Add fallback relaxation when fewer than 8 candidates remain.
  - First relax exposure cooldown until at least 8 candidates are available.
  - Do not relax watched, active wishlist, or current effective `not_interested`.
  - Return enough debug metadata to explain when cooldown was relaxed.
- [x] Add maybe-later recency/downrank behavior.
  - Recent `maybe_later` should downrank or temporarily suppress a movie.
  - Repeated `maybe_later` should apply stronger downrank than a single event.
  - `removed_from_wishlist` keeps maybe-later semantics.
  - Maybe-later must not deactivate candidate-pool rows in this slice.
- [x] Preserve the existing 4 exploit plus 4 explore mix after cooldown and maybe-later adjustments.
  - Exploit slots come from the highest-scoring remaining candidates after penalties/filters.
  - Explore slots use quality-bounded weighted sampling from the remaining explore pool.
- [x] Add focused backend tests.
  - New sessions are persisted on each recommendation request.
  - Production cooldown prevents recent exposed movies from immediately reappearing.
  - Cooldown relaxation happens only when the remaining candidate count is too small.
  - Fixed seed plus fixed cooldown is reproducible.
  - Recent/repeated maybe-later lowers reappearance likelihood without becoming a hard negative.
- [x] Section verification:
  - Run focused backend tests for recommendation cooldown, debug seed behavior, and maybe-later downrank.

### Section 4: Shared Movie Card UI

Why fourth: card UI should consume the fixed backend contracts before adding filters and debug controls.

- [x] Keep one shared card across Recommend, Wishlist, and Not interested views.
- [x] Load real poster images.
  - Use `poster_url` when available.
  - Keep stable dimensions so images do not shift card layout.
  - Fall back to the existing placeholder when missing or failed.
- [x] Improve director and cast formatting.
  - Render clean comma-separated director/cast text.
  - Avoid mojibake separators and overly long unwrapped rows.
  - Keep title, year, rating, score, and source visible.
- [x] Show score and source consistently wherever API data exists.
  - Recommendation cards always show normalized score and source label.
  - Wishlist cards show normalized score and source label when originating recommendation metadata exists.
  - Missing score/source fields should leave the row clean, not show placeholder clutter.
- [x] Replace text `Remove` actions with trash icon buttons.
  - Applies to Wishlist remove and Not interested remove.
  - Recommendation card `-` remains the `not_interested` feedback action.
  - Use a familiar trash/delete icon.
  - Include accessible label or title text such as `Remove`.
  - Keep button sizing stable in hover/focus action rows.
- [x] Keep processed-card behavior unchanged.
  - Processed recommendation cards stay visible.
  - Use muted/greyed styling or lower opacity.
  - Disable/hide actions that no longer apply.
  - Do not auto-refresh recommendations because a card was processed.
- [x] Section verification:
  - Run frontend build.
  - Browser-check Recommend card poster/source/person formatting.
  - Browser-check Wishlist score/source rendering.
  - Browser-check Wishlist and Not interested trash buttons preserve current semantics.

### Section 5: Add Watched Search And Layout UI

Why fifth: the backend search contract should be stable before changing the Add watched interaction.

- [x] Move search above the record form.
  - Search area should span full width above the form.
  - Search and form should not be side-by-side on desktop.
  - Candidate results can remain directly under the search bar.
- [x] Keep preselection behavior.
  - When opened from Recommend or Wishlist, preselect the movie.
  - Keep search area visible and usable after preselection.
  - User can search again and choose a different movie if the handoff was wrong.
- [x] Support direct Douban subject ID and URL in the Add watched search input.
  - The same input accepts title, plain subject ID, or Douban subject URL.
  - Display clear status when an ID cannot be resolved.
- [x] Keep existing form behavior.
  - Quality options remain `1080p`, `4K`, `Other`.
  - `Other` submits only custom text.
  - Sheet remains derived from watched date year.
  - Draft persists and clears only after successful submit.
  - Successful submit returns to the source tab when opened from another tab.
- [x] Section verification:
  - Run frontend build.
  - Browser-check Add watched search appears above the form.
  - Browser-check title search, direct subject ID, and Douban subject URL flows.
  - Browser-check handoff preselection still works from Recommend and Wishlist.

### Section 6: Wishlist And Not Interested Filters

Why sixth: filters are useful only after card metadata and list rendering are stable.

- [x] Add Wishlist filters.
  - Start with a compact text filter over visible/listed movie title, year, director, and cast.
  - Keep active-wishlist-only semantics.
  - Preserve pagination/infinite-load behavior.
- [x] Add Not interested filters.
  - Start with a compact text filter over visible/listed movie title, year, director, and cast.
  - Keep current effective not-interested semantics.
  - Preserve pagination/infinite-load behavior.
- [x] Avoid making a full analytics/search dashboard.
  - Filters are for quickly finding list items, not recommendation analysis.
- [x] Section verification:
  - Run frontend build.
  - Browser-check Wishlist filter on a multi-item list.
  - Browser-check Not interested filter on a multi-item list.

### Section 7: Frontend Recommendation Refresh And Debug Controls

Why seventh: the frontend should switch Refresh/debug behavior only after backend recommendation controls exist.

- [x] Replace temporary refresh seed behavior.
  - Current temporary behavior: Refresh uses fixed debug seed `24`.
  - Production behavior: Refresh requests a new session with no `seed` and the backend default exposure cooldown.
- [x] Add a debug-only recommendation controls surface.
  - Current debug behavior is a single toggle.
  - Debug mode disables poster loading and sends `exposure_cooldown_sessions=1&seed=42`.
  - Do not add visible strategy controls to the normal frontend.
- [x] Keep restore behavior unchanged.
  - Reload with cached current session fetches `GET /recommendations/{session_id}`.
  - Restore must not generate a new recommendation session.
  - Refresh success replaces the current recommendation session cache.
- [x] Section verification:
  - Run frontend build.
  - Browser-check restore with cached recommendation session does not generate a new session.
  - Browser-check production Refresh creates a new session without fixed seed.
  - Browser-check production Refresh does not immediately repeat movies exposed in the recent cooldown window.
  - Browser-check debug requests use `exposure_cooldown_sessions=1&seed=42` and do not load poster images.

## Later Targets

- [ ] Tune poster loading polish after real use.
- [ ] Add richer recommendation explanations/debug panels beyond the current debug mode.
- [ ] Add advanced Wishlist and Not interested filters if simple text filtering is not enough.
- [ ] Add longer-term maybe-later learning once enough interaction data exists.
- [ ] Repair existing bad person metadata in `movies.directors`, starting with pure-English director rows such as `疯狂的石头` / `1862151` where Douban detail metadata should preserve the local-language name.

## Final Verification

- [x] Run focused backend tests for all changed recommendation/API behavior.
- [x] Run frontend build.
- [x] Browser-check full integrated flow:
  - Recommendation-derived cards show `Recommend from {movie title}`, not `Recommend from unknown movie`, when source movie data exists.
  - Cards show real posters when `poster_url` exists and fall back cleanly when missing.
  - Director and cast formatting is readable.
  - Wishlist cards show score/source when originating recommendation data exists.
  - Wishlist remove uses a trash icon and still records removed-from-wishlist semantics.
  - Not interested remove uses a trash icon and still clears current not-interested state.
  - Add watched search appears above the form and supports title, subject ID, and Douban URL input.
  - Wishlist and Not interested filters narrow visible/listed rows without changing state semantics.
  - Restore with cached recommendation session does not generate a new session.
  - Production Refresh creates a new session without fixed seed.
  - Production Refresh does not immediately repeat movies exposed in the recent cooldown window.
  - Debug request uses `exposure_cooldown_sessions=1&seed=42` and does not load poster images.
