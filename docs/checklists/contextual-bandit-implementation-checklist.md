# Contextual Bandit Implementation Checklist

This checklist tracks the backend-only implementation of `bandit_hybrid`.
Do not change the frontend default recommendation entry point in this phase.

## Phase 0: Documentation And Contract

- [x] Choose Linear Thompson Sampling as the first contextual bandit algorithm.
- [x] Define `bandit_hybrid` as four hybrid exploit slots plus four bandit-ranked explore slots.
- [x] Keep `hybrid` as the API and frontend default.
- [x] Define `bandit_features_v1`.
- [x] Define `bandit_rewards_v1`.
- [x] Define rating reward: `rating < 4.0 -> -1.0`; `4.0 <= rating <= 5.0 -> rating - 4.0`.
- [x] Define trainable pre-watch rewards: `maybe_later = 0.05`, `want_to_watch = 0.10`, `not_interested = -1.0`.
- [x] Exclude opened-Douban, removed-from-wishlist, and clear-not-interested events from bandit v1 training.
- [x] Define freshness windows: `maybe_later` trains for 30 days; `want_to_watch` trains for 90 days.
- [x] Preserve serving constraints: exposure cooldown, maybe-later downranking, active not-interested exclusion, and batch-level diversity.

## Phase 1: Feature And Reward Builder

- [x] Add a backend feature builder for `bandit_features_v1`.
- [x] Reuse stable metadata families: genres, countries, directors, top actors, and decade.
- [x] Emit aggregate profile-match features instead of high-cardinality one-hot director or actor features.
- [x] Add `wishlist_similarity`.
- [x] Add `negative_feedback_similarity`.
- [x] Add source features: `source_is_top250`, `source_is_recommended_from_history`.
- [x] Add reward resolution for `bandit_rewards_v1`.
- [x] Ensure watched rating reward supersedes pre-watch feedback for the same recommendation item.
- [x] Ignore exposure-only items for v1 training.
- [x] Add unit tests for feature vector stability and reward priority.

## Phase 2: Linear Thompson Sampling

- [x] Implement diagonal Linear Thompson Sampling without new ML dependencies.
- [x] Fit from historical `recommendation_items` joined to feedback, sessions, movies, viewing history, and wishlist-derived state.
- [x] Allow historical `hybrid` sessions to train the bandit when feature and reward resolution is available.
- [x] Require at least 20 trainable examples before using bandit-ranked explore slots.
- [x] Fall back to current hybrid diversity explore logic when the training set is too small.
- [x] Support deterministic seeding for reproducible evaluation.
- [x] Add unit tests for posterior fitting, sampled scoring, seeding, and fallback behavior.

## Phase 3: Persistence And Debug Evidence

- [x] Populate `recommendation_sessions.context_snapshot` with `feature_version`, `reward_version`, `trainable_example_count`, `bandit_min_examples`, `bandit_used`, and fallback reason when relevant.
- [x] Populate bandit explore item `score_components` with `bandit_sample`, `bandit_mean`, `bandit_uncertainty`, and `feature_version`.
- [x] Keep recommendation facts and explanation evidence in SQL.
- [x] Write only a disposable latest-model cache to `.scratch/bandit/latest-model.json`.
- [x] Overwrite the local latest-model cache on update.
- [x] Ensure the local latest-model cache can be regenerated from PostgreSQL history.
- [x] Do not require the local latest-model cache for correctness.

## Phase 4: Recommendation Service Integration

- [x] Add `bandit_hybrid` as a backend strategy.
- [x] Keep `hybrid` as the default strategy.
- [x] Use hybrid ranker for exploit slots.
- [x] Use Linear Thompson Sampling for explore slots only.
- [x] Preserve candidate hard exclusions and active wishlist exclusion.
- [x] Preserve exposure cooldown.
- [x] Preserve 30-day maybe-later downranking.
- [x] Preserve active not-interested exclusion or strong item-level penalty.
- [x] Preserve batch-level diversity for explore slots.
- [x] Fall back to current hybrid explore logic if bandit training or snapshot handling fails.
- [x] Add service tests for `bandit_hybrid` slot mix, fallback metadata, and no watched leakage.

## Phase 5: Evaluation

- [x] Extend `jobs.evaluate_recommendations` to accept `--strategy bandit_hybrid`.
- [x] Report trainable example count and whether bandit was used or fallback was used.
- [x] Report repeated movie count across runs.
- [x] Report watched leakage.
- [x] Report duplicate-in-session count.
- [x] Report source mix.
- [x] Report slot mix.
- [x] Report explore-slot reward rate when historical feedback is available.
- [x] Report negative-feedback recurrence.
- [x] Compare `bandit_hybrid` against deterministic `hybrid` with the same seed.

## Phase 6: Review Gate

- [x] Run backend tests.
- [x] Run evaluator for `hybrid`.
- [x] Run evaluator for `bandit_hybrid`.
- [x] Review sample `context_snapshot` rows.
- [x] Review sample `score_components` rows.
- [x] Confirm backend behavior with the user before any frontend changes.
- [x] Do not change the frontend default in this phase.

## Review Evidence

- Backend tests: `.\.venv\Scripts\python.exe -m unittest` passed with 187 tests, 3 skipped.
- `hybrid` evaluator: 3 runs with seed 42, 24 total items, 24 unique movies, 0 duplicate-in-session, 0 watched leakage, 0 negative-feedback recurrence, average Douban rating 7.604.
- `bandit_hybrid` evaluator: 3 runs with seed 42, 24 total items, 24 unique movies, 0 duplicate-in-session, 0 watched leakage, 0 negative-feedback recurrence, average Douban rating 8.092.
- `bandit_hybrid` review run used 50 trainable examples, enabled the bandit in 3 of 3 runs, and did not fall back.
- Reviewed recent `recommendation_sessions.context_snapshot` rows for `feature_version`, `reward_version`, `trainable_example_count`, `bandit_min_examples`, and `bandit_used`.
- Reviewed recent bandit explore `recommendation_items.score_components` rows for `bandit_sample`, `bandit_mean`, `bandit_uncertainty`, and `feature_version`.
