from __future__ import annotations

import math
import random
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from backend.app.models.domain import (
    Feedback,
    FeedbackType,
    Movie,
    RecommendationItem,
    RecommendationSession,
    ViewingHistory,
    WishlistItem,
    WishlistStatus,
)

FEATURE_VERSION = "bandit_features_v1"
REWARD_VERSION = "bandit_rewards_v1"

FEATURE_NAMES: tuple[str, ...] = (
    "intercept",
    "hybrid_total",
    "content_score",
    "popularity_score",
    "novelty_score",
    "douban_rating_normalized",
    "log_vote_count_normalized",
    "genre_profile_match",
    "country_profile_match",
    "director_profile_match",
    "actor_profile_match",
    "decade_profile_match",
    "wishlist_similarity",
    "negative_feedback_similarity",
    "source_is_top250",
    "source_is_recommended_from_history",
    "maybe_later_penalty",
)

WANT_TO_WATCH_FRESHNESS = timedelta(days=90)
MAYBE_LATER_FRESHNESS = timedelta(days=30)
BANDIT_MIN_EXAMPLES = 20
LATEST_MODEL_CACHE_PATH = Path(".scratch/bandit/latest-model.json")


@dataclass(frozen=True)
class BanditFeatureVector:
    version: str
    names: tuple[str, ...]
    values: tuple[float, ...]

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.names, self.values))


@dataclass(frozen=True)
class BanditFeatureContext:
    positive_profiles: dict[str, Counter[str]]
    wishlist_profiles: dict[str, Counter[str]]
    negative_feedback_profiles: dict[str, Counter[str]]


@dataclass(frozen=True)
class BanditReward:
    version: str
    value: float
    source: str


@dataclass(frozen=True)
class BanditTrainingExample:
    session_id: str
    item_id: str
    movie_id: str
    strategy: str
    features: BanditFeatureVector
    reward: BanditReward


@dataclass(frozen=True)
class BanditScore:
    sample: float
    mean: float
    uncertainty: float


@dataclass(frozen=True)
class DiagonalLinearThompsonModel:
    feature_version: str
    feature_names: tuple[str, ...]
    posterior_mean: tuple[float, ...]
    posterior_precision: tuple[float, ...]
    trained_example_count: int

    @property
    def is_ready(self) -> bool:
        return self.trained_example_count >= BANDIT_MIN_EXAMPLES

    def mean_score(self, features: BanditFeatureVector) -> float:
        self._validate_features(features)
        return _dot(features.values, self.posterior_mean)

    def uncertainty(self, features: BanditFeatureVector) -> float:
        self._validate_features(features)
        variance = sum(
            (value * value) / precision
            for value, precision in zip(features.values, self.posterior_precision)
            if precision > 0
        )
        return math.sqrt(max(0.0, variance))

    def sampled_score(self, features: BanditFeatureVector, rng: random.Random) -> BanditScore:
        self._validate_features(features)
        sampled_weights = tuple(
            rng.gauss(mean, 1.0 / math.sqrt(precision))
            for mean, precision in zip(self.posterior_mean, self.posterior_precision)
        )
        return BanditScore(
            sample=_dot(features.values, sampled_weights),
            mean=self.mean_score(features),
            uncertainty=self.uncertainty(features),
        )

    def _validate_features(self, features: BanditFeatureVector) -> None:
        if features.version != self.feature_version or features.names != self.feature_names:
            raise ValueError("feature vector does not match bandit model")

    def to_snapshot(self, updated_at: datetime | None = None) -> dict[str, object]:
        timestamp = updated_at or datetime.now(timezone.utc)
        return {
            "strategy": "bandit_hybrid",
            "feature_version": self.feature_version,
            "trained_example_count": self.trained_example_count,
            "posterior_mean": list(self.posterior_mean),
            "posterior_precision": list(self.posterior_precision),
            "updated_at": _as_aware(timestamp).isoformat(),
        }


def build_bandit_feature_context(
    history: Iterable[ViewingHistory],
    movies_by_id: dict[str, Movie],
    wishlist: Iterable[WishlistItem] = (),
    feedback: Iterable[Feedback] = (),
) -> BanditFeatureContext:
    positive_profiles = _empty_profiles()
    wishlist_profiles = _empty_profiles()
    negative_feedback_profiles = _empty_profiles()

    for watched in history:
        movie = movies_by_id.get(watched.movie_id)
        if movie is None or watched.user_rating is None:
            continue
        if watched.user_rating >= 4.0:
            weight = 1.0 if watched.user_rating >= 4.5 else 0.6
            _update_profiles(positive_profiles, movie, weight)
        else:
            _update_profiles(negative_feedback_profiles, movie, 1.0)

    for item in wishlist:
        if item.status == WishlistStatus.ACTIVE:
            _update_profiles(wishlist_profiles, item.movie, 1.0)

    current_negative_movie_ids = _current_not_interested_movie_ids(feedback)
    for movie_id in current_negative_movie_ids:
        movie = movies_by_id.get(movie_id)
        if movie is not None:
            _update_profiles(negative_feedback_profiles, movie, 1.0)

    return BanditFeatureContext(
        positive_profiles=positive_profiles,
        wishlist_profiles=wishlist_profiles,
        negative_feedback_profiles=negative_feedback_profiles,
    )


def build_bandit_feature_vector(
    movie: Movie,
    score_components: dict[str, float],
    context: BanditFeatureContext,
    source_ref: str | None = None,
) -> BanditFeatureVector:
    values = (
        1.0,
        _score_value(score_components, "total"),
        _score_value(score_components, "personal_preference"),
        _score_value(score_components, "public_quality"),
        _score_value(score_components, "novelty"),
        movie.douban_rating / 10.0,
        math.log10(max(movie.douban_vote_count, 1)) / 7.0,
        _family_match(movie, context.positive_profiles, "genre"),
        _family_match(movie, context.positive_profiles, "country"),
        _family_match(movie, context.positive_profiles, "director"),
        _family_match(movie, context.positive_profiles, "actor"),
        _family_match(movie, context.positive_profiles, "decade"),
        _aggregate_similarity(movie, context.wishlist_profiles),
        _aggregate_similarity(movie, context.negative_feedback_profiles),
        1.0 if source_ref and source_ref.startswith("top") else 0.0,
        1.0 if source_ref and source_ref.startswith("recommended_from:") else 0.0,
        abs(_score_value(score_components, "maybe_later_penalty")),
    )
    return BanditFeatureVector(version=FEATURE_VERSION, names=FEATURE_NAMES, values=values)


def resolve_bandit_reward(
    item: RecommendationItem,
    feedback: Iterable[Feedback],
    history: Iterable[ViewingHistory],
    now: datetime | None = None,
) -> BanditReward | None:
    current_time = now or datetime.now(timezone.utc)
    watched = _latest_watched_rating(item.movie.id, history)
    if watched is not None:
        return BanditReward(version=REWARD_VERSION, value=_rating_reward(watched.user_rating), source="watched_rating")

    item_feedback = sorted(
        [event for event in feedback if event.item_id == item.id],
        key=lambda event: event.created_at,
        reverse=True,
    )
    if _fresh_feedback(item_feedback, FeedbackType.WANT_TO_WATCH, WANT_TO_WATCH_FRESHNESS, current_time):
        return BanditReward(version=REWARD_VERSION, value=0.10, source=FeedbackType.WANT_TO_WATCH.value)
    if _fresh_feedback(item_feedback, FeedbackType.MAYBE_LATER, MAYBE_LATER_FRESHNESS, current_time):
        return BanditReward(version=REWARD_VERSION, value=0.05, source=FeedbackType.MAYBE_LATER.value)
    if _current_item_state(item_feedback) == FeedbackType.NOT_INTERESTED:
        return BanditReward(version=REWARD_VERSION, value=-1.0, source=FeedbackType.NOT_INTERESTED.value)
    return None


def build_bandit_training_examples(
    sessions: Iterable[RecommendationSession],
    feedback: Iterable[Feedback],
    history: Iterable[ViewingHistory],
    movies_by_id: dict[str, Movie],
    wishlist: Iterable[WishlistItem] = (),
    now: datetime | None = None,
    trainable_strategies: set[str] | None = None,
) -> list[BanditTrainingExample]:
    strategies = trainable_strategies or {"hybrid", "bandit_hybrid"}
    feedback_items = list(feedback)
    history_items = list(history)
    context = build_bandit_feature_context(
        history=history_items,
        movies_by_id=movies_by_id,
        wishlist=wishlist,
        feedback=feedback_items,
    )
    examples: list[BanditTrainingExample] = []
    for session in sessions:
        if session.strategy not in strategies:
            continue
        for item in session.items:
            reward = resolve_bandit_reward(item, feedback_items, history_items, now=now)
            if reward is None:
                continue
            examples.append(
                BanditTrainingExample(
                    session_id=session.id,
                    item_id=item.id,
                    movie_id=item.movie.id,
                    strategy=session.strategy,
                    features=build_bandit_feature_vector(
                        item.movie,
                        item.score_components,
                        context,
                        source_ref=item.source_ref,
                    ),
                    reward=reward,
                )
            )
    return examples


def fit_diagonal_linear_thompson_model(
    examples: Iterable[BanditTrainingExample],
    prior_precision: float = 1.0,
) -> DiagonalLinearThompsonModel:
    if prior_precision <= 0:
        raise ValueError("prior_precision must be positive")
    example_list = list(examples)
    precision = [prior_precision for _ in FEATURE_NAMES]
    weighted_reward_sum = [0.0 for _ in FEATURE_NAMES]
    for example in example_list:
        if example.features.version != FEATURE_VERSION or example.features.names != FEATURE_NAMES:
            raise ValueError("training example feature vector does not match bandit feature version")
        reward = example.reward.value
        for index, value in enumerate(example.features.values):
            precision[index] += value * value
            weighted_reward_sum[index] += value * reward
    posterior_mean = tuple(weighted_reward_sum[index] / precision[index] for index in range(len(FEATURE_NAMES)))
    return DiagonalLinearThompsonModel(
        feature_version=FEATURE_VERSION,
        feature_names=FEATURE_NAMES,
        posterior_mean=posterior_mean,
        posterior_precision=tuple(precision),
        trained_example_count=len(example_list),
    )


def should_use_bandit_explore(model: DiagonalLinearThompsonModel) -> bool:
    return model.trained_example_count >= BANDIT_MIN_EXAMPLES


def seeded_bandit_scores(
    model: DiagonalLinearThompsonModel,
    features: Iterable[BanditFeatureVector],
    seed: int | None = None,
) -> list[BanditScore]:
    rng = random.Random(seed) if seed is not None else random.Random()
    return [model.sampled_score(feature, rng) for feature in features]


def write_latest_model_cache(
    model: DiagonalLinearThompsonModel,
    path: Path = LATEST_MODEL_CACHE_PATH,
    updated_at: datetime | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(model.to_snapshot(updated_at=updated_at), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _score_value(score_components: dict[str, float], key: str) -> float:
    return float(score_components.get(key, 0.0))


def _rating_reward(rating: float | None) -> float:
    if rating is None:
        raise ValueError("rating reward requires a rating")
    if rating < 4.0:
        return -1.0
    return min(1.0, max(0.0, rating - 4.0))


def _latest_watched_rating(movie_id: str, history: Iterable[ViewingHistory]) -> ViewingHistory | None:
    watched = [item for item in history if item.movie_id == movie_id and item.user_rating is not None]
    if not watched:
        return None
    return max(watched, key=lambda item: item.created_at)


def _fresh_feedback(
    feedback: list[Feedback],
    feedback_type: FeedbackType,
    freshness: timedelta,
    now: datetime,
) -> Feedback | None:
    for event in feedback:
        if event.feedback_type == feedback_type and now - _as_aware(event.created_at) <= freshness:
            return event
    return None


def _current_item_state(feedback: list[Feedback]) -> FeedbackType | None:
    for event in feedback:
        if event.feedback_type in {
            FeedbackType.WANT_TO_WATCH,
            FeedbackType.MAYBE_LATER,
            FeedbackType.NOT_INTERESTED,
            FeedbackType.REMOVED_FROM_WISHLIST,
            FeedbackType.CLEAR_NOT_INTERESTED,
        }:
            return event.feedback_type
    return None


def _current_not_interested_movie_ids(feedback: Iterable[Feedback]) -> set[str]:
    latest_by_movie: dict[str, Feedback] = {}
    for event in feedback:
        if event.feedback_type in {
            FeedbackType.WANT_TO_WATCH,
            FeedbackType.MAYBE_LATER,
            FeedbackType.NOT_INTERESTED,
            FeedbackType.REMOVED_FROM_WISHLIST,
            FeedbackType.CLEAR_NOT_INTERESTED,
        }:
            latest_by_movie[event.movie_id] = event
    return {
        event.movie_id
        for event in latest_by_movie.values()
        if event.feedback_type == FeedbackType.NOT_INTERESTED
    }


def _empty_profiles() -> dict[str, Counter[str]]:
    return {family: Counter() for family in ("genre", "country", "director", "actor", "decade")}


def _update_profiles(profiles: dict[str, Counter[str]], movie: Movie, weight: float) -> None:
    for family, values in _family_values(movie).items():
        profiles[family].update({value: weight for value in values})


def _aggregate_similarity(movie: Movie, profiles: dict[str, Counter[str]]) -> float:
    family_scores = [_family_match(movie, profiles, family) for family in profiles]
    return sum(family_scores) / max(len(family_scores), 1)


def _family_match(movie: Movie, profiles: dict[str, Counter[str]], family: str) -> float:
    values = _family_values(movie)[family]
    if not values:
        return 0.0
    profile = profiles[family]
    if not profile:
        return 0.0
    overlap = sum(profile[value] for value in values)
    max_possible = sum(sorted(profile.values(), reverse=True)[: len(values)])
    if max_possible <= 0:
        return 0.0
    return min(1.0, overlap / max_possible)


def _family_values(movie: Movie) -> dict[str, tuple[str, ...]]:
    return {
        "genre": tuple(movie.genres),
        "country": tuple(movie.countries),
        "director": tuple(movie.directors),
        "actor": tuple(movie.actors[:3]),
        "decade": (str(movie.decade),),
    }


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(left_value * right_value for left_value, right_value in zip(left, right))
