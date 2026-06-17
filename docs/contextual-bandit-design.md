# Contextual Bandit Recommendation Design

This document defines the first learning recommendation strategy for the movie recommender. It is not a true RL agent. It is an online ranking layer that uses accumulated recommendation feedback to improve the exploratory half of each recommendation session.

## Decision

Use Linear Thompson Sampling for the first contextual bandit implementation.

The first production shape should be `bandit_hybrid`:

```text
8 recommendation slots
= 4 exploit slots from the existing hybrid ranker
+ 4 explore slots ranked by Linear Thompson Sampling
```

The existing `hybrid` strategy remains the default baseline until the bandit has enough feedback history and evaluation evidence.

Bandit v1 controls only the four explore slots. The four exploit slots continue to come from the existing hybrid ranker. The frontend default should not automatically switch to `bandit_hybrid`; switching defaults requires manual approval after evaluation and real-use evidence.

The public strategy name is:

```text
bandit_hybrid
```

The API and frontend default remains:

```text
hybrid
```

`bandit_hybrid` should be invoked explicitly through the recommendation strategy parameter during evaluation and review.

## Why Not Full RL

The product is single-user, on-demand, and feedback is sparse. Full RL would require a reliable sequential state model, enough interaction volume, and delayed reward handling before it can beat the current hybrid baseline. A contextual bandit gives the system a practical learning loop without requiring a long-horizon policy.

## Existing Data Inputs

The current PostgreSQL-backed recommendation loop already records the minimum viable inputs:

- `recommendation_sessions`: one user-triggered recommendation batch, including strategy and `context_snapshot`
- `recommendation_items`: displayed movies, rank, slot type, score, `score_components`, and candidate source
- `feedback`: user feedback for a displayed recommendation item, including `feedback_type` and `feedback_value`
- `movies`: candidate metadata used to build item features
- `viewing_history`: watched history and user rating data used to build the user profile
- `wishlist`: medium-strength recent positive interest

The first implementation can train from displayed items only:

```text
training example =
  recommendation_item
  + optional feedback
  + movie metadata
  + session context snapshot
```

The system does not currently persist every non-displayed candidate considered during a session. That is acceptable for the first online bandit, but it limits strict offline counterfactual evaluation.

## Context

The context is the user state at the moment a recommendation session is generated.

Minimum context fields:

- positive feature profile from highly rated viewing history
- negative feature profile from low-rated viewing history
- recent wishlist feature profile
- recent maybe-later and not-interested feature profile
- active candidate count
- strategy and feature version

`recommendation_sessions.context_snapshot` should store a compact, versioned summary of these fields.

## Candidate Features

Each candidate movie should be converted into a fixed-length feature vector. The first version should prefer stable numeric features over high-cardinality sparse identifiers.

Do not use high-dimensional one-hot director or actor features in v1. Use aggregate profile-match scores instead so the model remains stable with sparse single-user feedback and can be implemented without new ML dependencies.

Recommended first feature vector:

```text
[
  1.0,                              # intercept
  hybrid_total,
  content_score,
  popularity_score,
  novelty_score,
  douban_rating_normalized,
  log_vote_count_normalized,
  genre_profile_match,
  country_profile_match,
  director_profile_match,
  actor_profile_match,
  decade_profile_match,
  wishlist_similarity,
  negative_feedback_similarity,
  source_is_top250,
  source_is_recommended_from_history,
  maybe_later_penalty
]
```

The feature builder should be deterministic and versioned. Store the feature version in `context_snapshot` and optionally in `score_components`.

The first feature version is:

```text
bandit_features_v1
```

Profile-match features should use the same stable metadata families as the existing content score: genres, countries, directors, top actors, and decade. Do not use embeddings or LLM-derived similarity in v1.

`negative_feedback_similarity` should measure overlap with active negative-feedback profiles across those metadata families. It should penalize similar movies through the model, not hard-exclude the whole neighborhood.

`wishlist_similarity` should measure overlap with the active wishlist profile across the same metadata families. Active wishlist movies remain excluded from recommendation candidates, but their metadata can still contribute to recent interest.

Candidate source should be represented with low-cardinality features:

```text
source_is_top250
source_is_recommended_from_history
```

Do not one-hot encode the specific source movie id in v1. If a candidate came from a watched movie's Douban recommendations, v1 should rely on `source_is_recommended_from_history` plus profile-match features rather than a separate source-movie-rating feature.

## Algorithm

Use a linear reward model:

```text
expected_reward = x dot theta
```

For each recommendation run:

1. Build the normal hybrid ranking.
2. Select the top 4 exploit items from hybrid ranking.
3. Exclude already selected items from the explore candidate set.
4. Fit or load the Linear Thompson Sampling posterior from historical feedback.
5. Sample candidate weights from the posterior.
6. Score remaining candidates with the sampled weights.
7. Select 4 explore items, preserving existing hard exclusions and batch-level diversity.
8. Save bandit scoring details in `score_components`.

The first implementation can fit from historical examples at recommendation time. With single-user data volume, this avoids a new model-state table at the start.

The service may keep one local latest-model snapshot for faster startup or debugging. This snapshot should be overwritten on update, not appended as an event log. The source of truth remains PostgreSQL recommendation sessions, recommendation items, feedback, viewing history, and movie metadata.

In v1, model fitting can happen before each recommendation request. The service reads trainable historical examples, fits the posterior, and overwrites the local latest-model snapshot. Historical `hybrid` sessions may be used for training if their displayed items have trainable feedback and the same feature/reward resolution can be applied.

## Posterior Shape

Start with a diagonal approximation unless there is a clear need for a full covariance matrix.

For each feature `i`, maintain:

```text
mean_i
precision_i
```

At selection time:

```text
sampled_weight_i ~ Normal(mean_i, 1 / sqrt(precision_i))
bandit_sample = sum(feature_i * sampled_weight_i)
```

The diagonal approximation is easier to implement and inspect. It can be replaced by a full Bayesian linear regression posterior later if the feature set stabilizes and `numpy` is accepted as a dependency.

## Reward Mapping

Reward must represent the user's actual satisfaction, not just clicks. Pre-watch feedback is useful but weaker than post-watch rating.

The first reward version is:

```text
bandit_rewards_v1
```

Recommended immediate feedback rewards:

```text
opened_douban          =  0.02   # logged, excluded from bandit v1 training
maybe_later            =  0.05
want_to_watch          =  0.10
not_interested         = -1.00
removed_from_wishlist  = -0.20  # logged, excluded from bandit v1 training
clear_not_interested   =  0.00  # state reset, excluded from bandit v1 training
```

`maybe_later` is only a very weak positive signal. It means the recommendation was not rejected, not that the movie is clearly preferred. `want_to_watch` is a stronger pre-watch intent signal, but both remain weaker than post-watch rating reward.

`maybe_later` is trainable as a very weak positive only while it is fresh. If it has not converted into `want_to_watch` or a watched movie after 30 days, ignore it for bandit training.

`want_to_watch` is trainable as a weak positive only while it is fresh. If it has not converted into a watched movie after 90 days, ignore it for bandit training.

`opened_douban` should be persisted as a behavior event but excluded from the first bandit training set. Opening the external page may reflect inspection rather than preference.

`removed_from_wishlist` should be persisted as a state/event transition but excluded from the first bandit training set. Removing a wishlist item can mean cleanup or changed intent, not necessarily negative preference for similar movies.

`clear_not_interested` should reset the hard negative state but should not become a positive training example.

Recommended post-watch rating reward:

```text
rating < 4.0           = -1.00
4.0 <= rating <= 5.0   = rating - 4.0
```

The positive rating reward is a linear mapping from the user's positive rating band to `[0, 1]`:

```text
rating_reward = rating - 4.0
```

Examples:

```text
4.0 -> 0.00
4.5 -> 0.50
5.0 -> 1.00
```

When a recommendation is later recorded as watched, the post-watch reward should supersede weaker pre-watch feedback for model training. The historical event log should remain append-only; reward resolution is a training-time interpretation.

Example:

```text
want_to_watch -> 0.10
later rating 3.8 -> -1.00
training reward = -1.00
```

Do not add pre-watch and post-watch rewards together for the same recommendation item.

## Personal Rating Semantics

The user's rating scale should be treated as selective and personal:

- below 4.0 means the movie was not worth watching
- 4.0 to 4.2 means good enough or pretty good
- 4.3 to 4.5 means very good
- above 4.5 means top-tier personal favorite
- watched movies are expected to have a rating

The bandit reward function should preserve this sharp distinction. In particular, every rating below `4.0` should train as strong negative feedback, while ratings from `4.0` to `5.0` should map linearly into the positive reward range.

## Training Example Resolution

For each displayed item, choose one training reward:

1. If the item was recorded as watched from the recommendation and has a rating, use the rating reward.
2. Else use fresh `want_to_watch` feedback.
3. Else use fresh `maybe_later` feedback.
4. Else use active `not_interested` feedback.
5. Else ignore the item for supervised fitting.

Training reward priority:

```text
watched rating > fresh want_to_watch > fresh maybe_later > active not_interested
```

Do not train the first bandit model on opened Douban, removed-from-wishlist, or clear-not-interested events.

Exposure-only items should not train as negative examples in v1. A missing feedback event can mean the user had no time or had not decided yet; it should not be interpreted as dislike.

`not_interested` does not expire. It remains a hard negative for the same movie until the user clears it. For similar movies, v1 should not hard-exclude the whole neighborhood; it should expose similarity to prior negative feedback as `negative_feedback_similarity` and let the model learn the penalty.

## Serving Constraints

Bandit training reward does not replace serving-time safety rules.

Keep both existing serving constraints:

- exposure cooldown: recently recommended movies should not reappear for the configured number of sessions when enough alternatives exist
- maybe-later downranking: a movie marked `maybe_later` should be less likely to reappear for 30 days, even though the event is a very weak positive training signal while fresh
- not-interested exclusion: a movie marked `not_interested` should remain excluded or strongly penalized until the user clears that state

These rules solve different problems. The bandit learns which movie features tend to satisfy the user. Exposure cooldown prevents repetitive sessions. Maybe-later downranking respects the user's item-level deferral.

## Persistence

No new database is required for the first implementation.

Use existing tables:

- read training examples from `recommendation_items` joined to `feedback`, `recommendation_sessions`, and `movies`
- write selected item scores to `recommendation_items.score_components`
- write context and feature metadata to `recommendation_sessions.context_snapshot`

Recommendation facts and explanation evidence must be persisted in SQL. `context_snapshot` and `score_components` belong with the session and item rows because they explain an already-generated recommendation. The local latest-model snapshot is only a regenerable cache of the current posterior.

Minimum `context_snapshot` fields for `bandit_hybrid`:

```json
{
  "feature_version": "bandit_features_v1",
  "reward_version": "bandit_rewards_v1",
  "trainable_example_count": 42,
  "bandit_min_examples": 20,
  "bandit_used": true
}
```

Minimum `score_components` fields for bandit-ranked explore items:

```json
{
  "bandit_sample": 0.31,
  "bandit_mean": 0.22,
  "bandit_uncertainty": 0.09,
  "feature_version": "bandit_features_v1"
}
```

Persist in SQL:

```text
recommendation_sessions.strategy
recommendation_sessions.context_snapshot
recommendation_items.score
recommendation_items.score_components
feedback.feedback_type
feedback.feedback_value
viewing_history.rating
```

Persist in the local latest-model cache:

```text
posterior_mean
posterior_precision
trained_example_count
feature_version
updated_at
```

Optional local latest-model snapshot:

```text
.scratch/bandit/latest-model.json
  strategy
  feature_version
  trained_example_count
  posterior_mean
  posterior_precision
  updated_at
```

This file is a disposable cache and debug artifact. It can be regenerated from PostgreSQL history.
It should not be committed.

Optional later table:

```text
bandit_model_snapshots
  id uuid primary key
  strategy text
  feature_version text
  model_state jsonb
  trained_example_count integer
  created_at timestamptz
```

Add this only if fitting on demand becomes too slow or if model replay/debugging needs a persisted snapshot.

## Evaluation

Extend `jobs.evaluate_recommendations` before switching the frontend default from `hybrid` to `bandit_hybrid`.

The bandit should not become eligible for exploit slots through an automatic threshold. It can be considered for broader control only after evaluation and real-use evidence support a manual product decision.

`bandit_hybrid` should support deterministic seeding for evaluation and reproduction:

```text
GET /recommendations?strategy=bandit_hybrid&seed=42
```

If there are fewer than 20 trainable examples, `bandit_hybrid` should fall back to the current hybrid diversity explore logic for the explore slots.

If bandit training or local snapshot handling fails, the recommendation request should fall back to the current hybrid diversity explore logic and persist the fallback in `context_snapshot`:

```json
{
  "bandit_used": false,
  "bandit_fallback_reason": "training_failed"
}
```

Implementation should start with backend strategy support and evaluator coverage. Do not change the frontend default entry point before backend behavior has been reviewed.

Use `docs/checklists/contextual-bandit-implementation-checklist.md` to track backend implementation.

Minimum reports:

- repeated movie count across runs
- watched leakage
- duplicate-in-session count
- source mix
- slot mix
- explore-slot reward rate
- negative-feedback recurrence
- comparison against deterministic `hybrid`

## Open Questions For Grilling

1. Should a missing rating after watched-from-recommendation be positive enough to train on?
2. Should `want_to_watch` be allowed to outweigh a later low rating?
3. Should bandit control only explore slots forever, or become eligible for exploit slots after enough evidence?
4. Should exposure-only no-feedback items be ignored or used as weak negative examples after an age threshold?
