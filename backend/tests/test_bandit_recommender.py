from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from backend.app.models.domain import (
    Feedback,
    FeedbackType,
    Movie,
    RecommendationItem,
    RecommendationSession,
    SlotType,
    ViewingHistory,
    WishlistItem,
)
from backend.app.recommenders.bandit import (
    BANDIT_MIN_EXAMPLES,
    FEATURE_NAMES,
    FEATURE_VERSION,
    REWARD_VERSION,
    build_bandit_feature_context,
    build_bandit_feature_vector,
    build_bandit_training_examples,
    fit_diagonal_linear_thompson_model,
    resolve_bandit_reward,
    seeded_bandit_scores,
    should_use_bandit_explore,
    write_latest_model_cache,
)


class BanditRecommenderTest(unittest.TestCase):
    def test_feature_vector_is_stable_and_uses_aggregate_profile_matches(self) -> None:
        liked = _movie(
            "liked",
            genres=("Drama",),
            countries=("Japan",),
            directors=("Director A",),
            actors=("Actor A", "Actor B"),
            year=1999,
        )
        disliked = _movie(
            "disliked",
            genres=("Horror",),
            countries=("United States",),
            directors=("Director B",),
            actors=("Actor C",),
            year=1984,
        )
        candidate = _movie(
            "candidate",
            genres=("Drama", "Horror"),
            countries=("Japan",),
            directors=("Director A",),
            actors=("Actor A", "Actor Z"),
            year=1995,
            douban_rating=8.4,
            douban_vote_count=1_000_000,
        )
        movies_by_id = {movie.id: movie for movie in (liked, disliked, candidate)}
        context = build_bandit_feature_context(
            history=[
                ViewingHistory(movie_id=liked.id, watched_date=date(2025, 1, 1), user_rating=4.6),
                ViewingHistory(movie_id=disliked.id, watched_date=date(2025, 1, 2), user_rating=3.0),
            ],
            movies_by_id=movies_by_id,
            wishlist=[WishlistItem(movie=liked, source_session_id="session-1")],
            feedback=[
                Feedback(
                    session_id="session-1",
                    item_id="item-1",
                    movie_id=disliked.id,
                    feedback_type=FeedbackType.NOT_INTERESTED,
                    feedback_value=-0.8,
                )
            ],
        )

        vector = build_bandit_feature_vector(
            candidate,
            {
                "total": 4.2,
                "personal_preference": 0.5,
                "public_quality": 8.0,
                "novelty": 0.3,
                "maybe_later_penalty": -1.5,
            },
            context,
            source_ref="recommended_from:1292434",
        )

        features = vector.as_dict()
        self.assertEqual(FEATURE_VERSION, vector.version)
        self.assertEqual(FEATURE_NAMES, vector.names)
        self.assertEqual(17, len(vector.values))
        self.assertNotIn("director:Director A", features)
        self.assertNotIn("actor:Actor A", features)
        self.assertEqual(1.0, features["intercept"])
        self.assertEqual(4.2, features["hybrid_total"])
        self.assertAlmostEqual(0.84, features["douban_rating_normalized"])
        self.assertGreater(features["genre_profile_match"], 0.0)
        self.assertGreater(features["director_profile_match"], 0.0)
        self.assertGreater(features["actor_profile_match"], 0.0)
        self.assertGreater(features["wishlist_similarity"], 0.0)
        self.assertGreater(features["negative_feedback_similarity"], 0.0)
        self.assertEqual(0.0, features["source_is_top250"])
        self.assertEqual(1.0, features["source_is_recommended_from_history"])
        self.assertEqual(1.5, features["maybe_later_penalty"])

    def test_reward_resolution_uses_watched_rating_before_pre_watch_feedback(self) -> None:
        item = _recommendation_item(_movie("candidate"))
        now = datetime(2026, 6, 15, tzinfo=timezone.utc)

        reward = resolve_bandit_reward(
            item,
            feedback=[
                Feedback(
                    session_id="session-1",
                    item_id=item.id,
                    movie_id=item.movie.id,
                    feedback_type=FeedbackType.WANT_TO_WATCH,
                    feedback_value=0.7,
                    created_at=now - timedelta(days=1),
                )
            ],
            history=[
                ViewingHistory(
                    movie_id=item.movie.id,
                    watched_date=date(2026, 6, 14),
                    user_rating=3.8,
                    created_at=now,
                )
            ],
            now=now,
        )

        self.assertIsNotNone(reward)
        assert reward is not None
        self.assertEqual(REWARD_VERSION, reward.version)
        self.assertEqual(-1.0, reward.value)
        self.assertEqual("watched_rating", reward.source)

    def test_reward_resolution_ignores_exposure_only_and_excluded_feedback_types(self) -> None:
        item = _recommendation_item(_movie("candidate"))
        now = datetime(2026, 6, 15, tzinfo=timezone.utc)

        self.assertIsNone(resolve_bandit_reward(item, feedback=[], history=[], now=now))
        self.assertIsNone(
            resolve_bandit_reward(
                item,
                feedback=[
                    Feedback(
                        session_id="session-1",
                        item_id=item.id,
                        movie_id=item.movie.id,
                        feedback_type=FeedbackType.OPENED_DOUBAN,
                        feedback_value=0.1,
                        created_at=now,
                    )
                ],
                history=[],
                now=now,
            )
        )

    def test_reward_resolution_applies_freshness_and_current_not_interested_state(self) -> None:
        item = _recommendation_item(_movie("candidate"))
        now = datetime(2026, 6, 15, tzinfo=timezone.utc)

        old_maybe = resolve_bandit_reward(
            item,
            feedback=[
                Feedback(
                    session_id="session-1",
                    item_id=item.id,
                    movie_id=item.movie.id,
                    feedback_type=FeedbackType.MAYBE_LATER,
                    feedback_value=0.2,
                    created_at=now - timedelta(days=31),
                )
            ],
            history=[],
            now=now,
        )
        current_negative = resolve_bandit_reward(
            item,
            feedback=[
                Feedback(
                    session_id="session-1",
                    item_id=item.id,
                    movie_id=item.movie.id,
                    feedback_type=FeedbackType.NOT_INTERESTED,
                    feedback_value=-0.8,
                    created_at=now,
                )
            ],
            history=[],
            now=now,
        )
        cleared_negative = resolve_bandit_reward(
            item,
            feedback=[
                Feedback(
                    session_id="session-1",
                    item_id=item.id,
                    movie_id=item.movie.id,
                    feedback_type=FeedbackType.NOT_INTERESTED,
                    feedback_value=-0.8,
                    created_at=now - timedelta(minutes=1),
                ),
                Feedback(
                    session_id="session-1",
                    item_id=item.id,
                    movie_id=item.movie.id,
                    feedback_type=FeedbackType.CLEAR_NOT_INTERESTED,
                    feedback_value=0.0,
                    created_at=now,
                ),
            ],
            history=[],
            now=now,
        )

        self.assertIsNone(old_maybe)
        self.assertIsNotNone(current_negative)
        assert current_negative is not None
        self.assertEqual(-1.0, current_negative.value)
        self.assertIsNone(cleared_negative)

    def test_training_examples_include_historical_hybrid_sessions_with_resolved_rewards(self) -> None:
        movie = _movie("candidate")
        item = _recommendation_item(movie)
        session = RecommendationSession(strategy="hybrid", items=[item], id="session-1")
        now = datetime(2026, 6, 15, tzinfo=timezone.utc)

        examples = build_bandit_training_examples(
            sessions=[session, RecommendationSession(strategy="popularity", items=[item], id="session-2")],
            feedback=[
                Feedback(
                    session_id=session.id,
                    item_id=item.id,
                    movie_id=movie.id,
                    feedback_type=FeedbackType.WANT_TO_WATCH,
                    feedback_value=0.7,
                    created_at=now,
                )
            ],
            history=[],
            movies_by_id={movie.id: movie},
            now=now,
        )

        self.assertEqual(1, len(examples))
        self.assertEqual("hybrid", examples[0].strategy)
        self.assertEqual(item.id, examples[0].item_id)
        self.assertEqual(0.10, examples[0].reward.value)
        self.assertEqual(FEATURE_VERSION, examples[0].features.version)

    def test_diagonal_linear_thompson_model_fits_positive_and_negative_examples(self) -> None:
        positive = _training_example("positive", reward=1.0, hybrid_total=2.0)
        negative = _training_example("negative", reward=-1.0, hybrid_total=-1.0)

        model = fit_diagonal_linear_thompson_model([positive, negative])

        feature_names = positive.features.names
        intercept_index = feature_names.index("intercept")
        hybrid_index = feature_names.index("hybrid_total")
        self.assertEqual(2, model.trained_example_count)
        self.assertEqual(FEATURE_VERSION, model.feature_version)
        self.assertAlmostEqual(0.0, model.posterior_mean[intercept_index])
        self.assertGreater(model.posterior_mean[hybrid_index], 0.0)
        self.assertGreater(model.mean_score(positive.features), model.mean_score(negative.features))
        self.assertGreater(model.uncertainty(positive.features), 0.0)

    def test_seeded_bandit_scores_are_reproducible(self) -> None:
        examples = [_training_example(f"example-{index}", reward=1.0, hybrid_total=1.0) for index in range(3)]
        model = fit_diagonal_linear_thompson_model(examples)
        features = [example.features for example in examples]

        first = seeded_bandit_scores(model, features, seed=42)
        repeated = seeded_bandit_scores(model, features, seed=42)
        different = seeded_bandit_scores(model, features, seed=43)

        self.assertEqual([score.sample for score in first], [score.sample for score in repeated])
        self.assertNotEqual([score.sample for score in first], [score.sample for score in different])

    def test_bandit_explore_requires_minimum_trainable_examples(self) -> None:
        small_model = fit_diagonal_linear_thompson_model(
            [_training_example(f"small-{index}", reward=0.1, hybrid_total=1.0) for index in range(BANDIT_MIN_EXAMPLES - 1)]
        )
        ready_model = fit_diagonal_linear_thompson_model(
            [_training_example(f"ready-{index}", reward=0.1, hybrid_total=1.0) for index in range(BANDIT_MIN_EXAMPLES)]
        )

        self.assertFalse(should_use_bandit_explore(small_model))
        self.assertFalse(small_model.is_ready)
        self.assertTrue(should_use_bandit_explore(ready_model))
        self.assertTrue(ready_model.is_ready)

    def test_latest_model_cache_is_overwritten_from_model_snapshot(self) -> None:
        first = fit_diagonal_linear_thompson_model([_training_example("first", reward=0.1, hybrid_total=1.0)])
        second = fit_diagonal_linear_thompson_model(
            [_training_example(f"second-{index}", reward=0.2, hybrid_total=2.0) for index in range(2)]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "latest-model.json"

            write_latest_model_cache(first, path=path, updated_at=datetime(2026, 6, 15, tzinfo=timezone.utc))
            write_latest_model_cache(second, path=path, updated_at=datetime(2026, 6, 16, tzinfo=timezone.utc))

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual("bandit_hybrid", payload["strategy"])
        self.assertEqual(FEATURE_VERSION, payload["feature_version"])
        self.assertEqual(2, payload["trained_example_count"])
        self.assertEqual(list(second.posterior_mean), payload["posterior_mean"])
        self.assertEqual(list(second.posterior_precision), payload["posterior_precision"])
        self.assertEqual("2026-06-16T00:00:00+00:00", payload["updated_at"])


def _recommendation_item(movie: Movie) -> RecommendationItem:
    return RecommendationItem(
        movie=movie,
        rank=1,
        slot_type=SlotType.EXPLORE,
        score=1.0,
        score_components={"total": 1.0},
        id="item-1",
    )


def _training_example(movie_id: str, reward: float, hybrid_total: float):
    from backend.app.recommenders.bandit import BanditReward, BanditTrainingExample

    movie = _movie(movie_id)
    item = _recommendation_item(movie)
    context = build_bandit_feature_context(history=[], movies_by_id={movie.id: movie})
    features = build_bandit_feature_vector(
        movie,
        {"total": hybrid_total, "personal_preference": hybrid_total, "public_quality": 0.0, "novelty": 0.0},
        context,
    )
    return BanditTrainingExample(
        session_id="session-1",
        item_id=item.id,
        movie_id=movie.id,
        strategy="hybrid",
        features=features,
        reward=BanditReward(version=REWARD_VERSION, value=reward, source="test"),
    )


def _movie(
    movie_id: str,
    genres: tuple[str, ...] = ("Drama",),
    countries: tuple[str, ...] = ("Japan",),
    directors: tuple[str, ...] = ("Director",),
    actors: tuple[str, ...] = ("Actor",),
    year: int = 2000,
    douban_rating: float = 8.0,
    douban_vote_count: int = 1000,
) -> Movie:
    return Movie(
        id=movie_id,
        title=movie_id,
        year=year,
        directors=directors,
        actors=actors,
        genres=genres,
        countries=countries,
        douban_rating=douban_rating,
        douban_vote_count=douban_vote_count,
        douban_url=f"https://movie.douban.com/subject/{movie_id}/",
    )


if __name__ == "__main__":
    unittest.main()
