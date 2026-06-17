from datetime import date
import os
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from backend.app.models.domain import (
    Feedback,
    FeedbackType,
    Movie,
    RecommendationItem,
    RecommendationProcessingStatus,
    RecommendationSession,
    SlotType,
    ViewingHistory,
    WishlistStatus,
)
from backend.app.recommenders.simple import build_content_profile
from backend.app.services.recommendation_service import (
    FeedbackRequest,
    InMemoryMovieRepository,
    PostgresRecommendationRepository,
    RecommendationCandidate,
    RecommendationService,
    RecordWatchedRequest,
    create_recommendation_service,
)


class RecommendationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryMovieRepository()
        self.service = RecommendationService(self.repository)

    def test_hybrid_recommendation_returns_four_exploit_and_four_explore(self) -> None:
        session = self.service.recommend("hybrid")

        self.assertEqual(8, len(session.items))
        self.assertEqual([1, 2, 3, 4, 5, 6, 7, 8], [item.rank for item in session.items])
        self.assertEqual(4, sum(1 for item in session.items if item.slot_type == SlotType.EXPLOIT))
        self.assertEqual(4, sum(1 for item in session.items if item.slot_type == SlotType.EXPLORE))
        self.assertEqual(8, len({item.movie.id for item in session.items}))

    def test_bandit_hybrid_falls_back_to_hybrid_explore_when_training_set_is_too_small(self) -> None:
        session = self.service.recommend("bandit_hybrid", explore_seed=3)

        self.assertEqual("bandit_hybrid", session.strategy)
        self.assertEqual(8, len(session.items))
        self.assertEqual(4, sum(1 for item in session.items if item.slot_type == SlotType.EXPLOIT))
        self.assertEqual(4, sum(1 for item in session.items if item.slot_type == SlotType.EXPLORE))
        self.assertFalse(session.debug_metadata["bandit_used"])
        self.assertEqual("insufficient_training_examples", session.debug_metadata["bandit_fallback_reason"])
        self.assertEqual(0, session.debug_metadata["trainable_example_count"])
        self.assertTrue(
            all("bandit_sample" not in item.score_components for item in session.items if item.slot_type == SlotType.EXPLORE)
        )

    def test_bandit_hybrid_uses_bandit_ranked_explore_slots_when_training_set_is_ready(self) -> None:
        repository = InMemoryMovieRepository(movies=_many_movies(40), history=[])
        service = RecommendationService(repository)
        _seed_bandit_training_history(repository, count=20)

        session = service.recommend("bandit_hybrid", explore_seed=11, exposure_cooldown_sessions=0)

        self.assertEqual("bandit_hybrid", session.strategy)
        self.assertTrue(session.debug_metadata["bandit_used"])
        self.assertEqual(20, session.debug_metadata["trainable_example_count"])
        self.assertEqual("bandit_features_v1", session.debug_metadata["feature_version"])
        self.assertEqual("bandit_rewards_v1", session.debug_metadata["reward_version"])
        self.assertEqual([SlotType.EXPLOIT] * 4, [item.slot_type for item in session.items[:4]])
        self.assertEqual([SlotType.EXPLORE] * 4, [item.slot_type for item in session.items[4:]])
        for item in session.items[4:]:
            self.assertIn("bandit_sample", item.score_components)
            self.assertIn("bandit_mean", item.score_components)
            self.assertIn("bandit_uncertainty", item.score_components)
            self.assertEqual("bandit_features_v1", item.score_components["feature_version"])

    def test_bandit_hybrid_does_not_leak_watched_movies(self) -> None:
        movies = _many_movies(40)
        watched_movie = movies[30]
        repository = InMemoryMovieRepository(
            movies=movies,
            history=[ViewingHistory(movie_id=watched_movie.id, watched_date=date(2026, 1, 1), user_rating=4.5)],
        )
        service = RecommendationService(repository)
        _seed_bandit_training_history(repository, count=20)

        session = service.recommend("bandit_hybrid", explore_seed=11, exposure_cooldown_sessions=0)

        self.assertNotIn(watched_movie.id, {item.movie.id for item in session.items})

    def test_bandit_hybrid_does_not_require_latest_model_cache_for_correctness(self) -> None:
        repository = InMemoryMovieRepository(movies=_many_movies(40), history=[])
        service = RecommendationService(repository)
        _seed_bandit_training_history(repository, count=20)

        with patch(
            "backend.app.services.recommendation_service.write_latest_model_cache",
            side_effect=OSError("cache unavailable"),
        ):
            session = service.recommend("bandit_hybrid", explore_seed=11, exposure_cooldown_sessions=0)

        self.assertTrue(session.debug_metadata["bandit_used"])
        self.assertNotIn("bandit_fallback_reason", session.debug_metadata)

    def test_hybrid_recommendation_builds_content_profile_once(self) -> None:
        with patch(
            "backend.app.services.recommendation_service.build_content_profile",
            wraps=build_content_profile,
        ) as build_profile:
            self.service.recommend("hybrid")

        build_profile.assert_called_once_with(self.repository.history, self.repository.movies_by_id)

    def test_recommendation_excludes_movies_newer_than_current_year(self) -> None:
        future_movie = Movie(
            id="m-future",
            title="Future Movie",
            year=date.today().year + 1,
            directors=("Future Director",),
            actors=("Future Actor",),
            genres=("Drama",),
            countries=("Japan",),
            douban_rating=10.0,
            douban_vote_count=10000000,
            douban_url="https://movie.douban.com/subject/9999999/",
        )
        self.repository.movies_by_id[future_movie.id] = future_movie
        self.repository.candidate_pool[future_movie.id] = RecommendationCandidate(movie=future_movie)

        session = self.service.recommend("hybrid", exposure_cooldown_sessions=0)

        self.assertNotIn(future_movie.id, [item.movie.id for item in session.items])

    def test_explore_seed_makes_session_reproducible_without_changing_exploit_slots(self) -> None:
        first = self.service.recommend("hybrid", explore_seed=1)
        repeated = self.service.recommend("hybrid", explore_seed=1)
        different = self.service.recommend("hybrid", explore_seed=2)

        self.assertEqual(
            [item.movie.id for item in first.items],
            [item.movie.id for item in repeated.items],
        )
        self.assertEqual(
            [item.movie.id for item in first.items[:4]],
            [item.movie.id for item in different.items[:4]],
        )

    def test_session_response_includes_processing_state_and_source_label(self) -> None:
        movie_id = "m-after-life"
        movie = self.repository.candidate_pool[movie_id].movie
        self.repository.candidate_pool[movie_id] = RecommendationCandidate(
            movie=movie,
            source_ref="recommended_from:1292434",
            source_label="recommended from Yi Yi",
        )

    def test_recommendation_source_label_resolves_source_subject_movie_when_stored_label_missing(self) -> None:
        movie_id = "m-after-life"
        movie = self.repository.candidate_pool[movie_id].movie
        self.repository.candidate_pool[movie_id] = RecommendationCandidate(
            movie=movie,
            source_ref="recommended_from:1292434",
            source_label=None,
        )

        session = self.service.recommend("hybrid")
        response = self.service.to_session_response(session)

        item = next(item for item in response["items"] if item["movie"]["id"] == movie_id)
        self.assertEqual("Recommend from Yi Yi", item["source_label"])

    def test_session_response_repairs_persisted_unknown_recommendation_source_label(self) -> None:
        session = self.service.recommend("hybrid")
        item = session.items[0]
        item.source_ref = "recommended_from:1292434"
        item.source_label = "Recommend from unknown movie"

        response = self.service.to_session_response(session)

        response_item = next(candidate for candidate in response["items"] if candidate["id"] == item.id)
        self.assertEqual("Recommend from Yi Yi", response_item["source_label"])

    def test_recommendation_source_label_falls_back_only_when_source_movie_missing(self) -> None:
        movie_id = "m-after-life"
        movie = self.repository.candidate_pool[movie_id].movie
        self.repository.candidate_pool[movie_id] = RecommendationCandidate(
            movie=movie,
            source_ref="recommended_from:missing",
            source_label=None,
        )

        session = self.service.recommend("hybrid")
        response = self.service.to_session_response(session)

        item = next(item for item in response["items"] if item["movie"]["id"] == movie_id)
        self.assertEqual("Recommend from unknown movie", item["source_label"])

    def test_movie_response_includes_poster_and_normalized_people_fields(self) -> None:
        movie = Movie(
            id="movie-1",
            title="Movie One",
            year=2001,
            directors=("雷德利·斯科特 Ridley Scott", "是枝裕和 Hirokazu Kore-eda"),
            actors=(
                "蒂姆·罗宾斯 Tim Robbins",
                "摩根·弗里曼 Morgan Freeman",
                "役所广司 Koji Yakusho",
                "夏川结衣 Yui Natsukawa",
            ),
            genres=("Drama",),
            countries=("Japan",),
            douban_rating=8.1,
            douban_vote_count=1000,
            douban_url="https://movie.douban.com/subject/1001/",
            poster_url="https://img.example/poster.webp",
        )

        response = self.service._movie_response(movie)

        self.assertEqual("Ridley Scott, 是枝裕和", response["director"])
        self.assertEqual(["Ridley Scott", "是枝裕和"], response["directors"])
        self.assertEqual(["Tim Robbins", "Morgan Freeman", "役所广司"], response["main_cast"])
        self.assertEqual(["Tim Robbins", "Morgan Freeman", "役所广司", "夏川结衣"], response["cast"])
        self.assertEqual("https://img.example/poster.webp", response["poster_url"])

    def test_get_session_returns_existing_recommendation_session(self) -> None:
        session = self.service.recommend("hybrid")

        restored = self.service.get_session(session.id)

        self.assertEqual(session.id, restored.id)
        self.assertEqual([item.id for item in session.items], [item.id for item in restored.items])

    def test_recommendation_requests_persist_new_sessions(self) -> None:
        first = self.service.recommend("hybrid", exposure_cooldown_sessions=0)
        second = self.service.recommend("hybrid", exposure_cooldown_sessions=0)

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(2, len(self.repository.sessions))

    def test_exposure_cooldown_excludes_recently_shown_movies(self) -> None:
        repository = InMemoryMovieRepository(movies=_many_movies(16), history=[])
        service = RecommendationService(repository)
        first = service.recommend("hybrid", explore_seed=1, exposure_cooldown_sessions=0)

        second = service.recommend("hybrid", explore_seed=1, exposure_cooldown_sessions=1)

        self.assertEqual(set(), {item.movie.id for item in first.items} & {item.movie.id for item in second.items})
        self.assertEqual(1, second.debug_metadata["applied_exposure_cooldown_sessions"])
        self.assertFalse(second.debug_metadata["cooldown_relaxed"])

    def test_exposure_cooldown_relaxes_when_too_few_candidates_remain(self) -> None:
        self.service.recommend("hybrid", exposure_cooldown_sessions=0)

        second = self.service.recommend("hybrid", explore_seed=1, exposure_cooldown_sessions=5)

        self.assertEqual(8, len(second.items))
        self.assertEqual(0, second.debug_metadata["applied_exposure_cooldown_sessions"])
        self.assertTrue(second.debug_metadata["cooldown_relaxed"])

    def test_fixed_seed_and_fixed_cooldown_are_reproducible(self) -> None:
        first_repository = InMemoryMovieRepository(movies=_many_movies(16), history=[])
        second_repository = InMemoryMovieRepository(movies=_many_movies(16), history=[])
        first_service = RecommendationService(first_repository)
        second_service = RecommendationService(second_repository)
        first_service.recommend("hybrid", explore_seed=3, exposure_cooldown_sessions=0)
        second_service.recommend("hybrid", explore_seed=3, exposure_cooldown_sessions=0)

        first = first_service.recommend("hybrid", explore_seed=7, exposure_cooldown_sessions=1)
        second = second_service.recommend("hybrid", explore_seed=7, exposure_cooldown_sessions=1)

        self.assertEqual([item.movie.id for item in first.items], [item.movie.id for item in second.items])

    def test_maybe_later_penalty_is_repeatable_without_hard_excluding_movie(self) -> None:
        session = self.service.recommend("hybrid", exposure_cooldown_sessions=0)
        item = session.items[0]
        base_total = self.service._score(item.movie, "hybrid")["total"]

        self.service.submit_feedback(
            session.id,
            item.id,
            FeedbackRequest(feedback_type=FeedbackType.MAYBE_LATER),
        )
        once = self.service._score_with_feedback_penalty(item.movie, "hybrid")
        self.service.submit_feedback(
            session.id,
            item.id,
            FeedbackRequest(feedback_type=FeedbackType.MAYBE_LATER),
        )
        twice = self.service._score_with_feedback_penalty(item.movie, "hybrid")

        self.assertEqual(base_total - 1.5, once["total"])
        self.assertEqual(base_total - 3.0, twice["total"])
        self.assertIn(item.movie.id, self.repository.candidate_pool)

    def test_maybe_later_penalty_expires_after_thirty_days(self) -> None:
        session = self.service.recommend("hybrid", exposure_cooldown_sessions=0)
        item = session.items[0]
        base_total = self.service._score(item.movie, "hybrid")["total"]
        self.service.submit_feedback(
            session.id,
            item.id,
            FeedbackRequest(feedback_type=FeedbackType.MAYBE_LATER),
        )
        self.repository.feedback[-1].created_at = datetime.now(timezone.utc) - timedelta(days=31)

        score = self.service._score_with_feedback_penalty(item.movie, "hybrid")

        self.assertEqual(base_total, score["total"])

    def test_want_to_watch_adds_wishlist_and_excludes_movie_from_future_sessions(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]

        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.WANT_TO_WATCH),
        )
        active_wishlist = list(self.repository.wishlist.values())
        self.assertEqual(1, len(active_wishlist))
        self.assertEqual(first_item.movie.id, active_wishlist[0].movie.id)
        self.assertEqual(RecommendationProcessingStatus.ADDED_TO_WISHLIST, first_item.processing_status)
        self.assertIsNotNone(first_item.processed_at)
        self.assertNotIn(first_item.movie.id, {candidate.movie.id for candidate in self.repository.active_candidates()})

    def test_not_interested_marks_item_and_deactivates_candidate_pool_row(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]

        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.NOT_INTERESTED),
        )

        self.assertEqual(RecommendationProcessingStatus.NOT_INTERESTED, first_item.processing_status)
        self.assertIsNotNone(first_item.processed_at)
        self.assertNotIn(first_item.movie.id, self.repository.candidate_pool)
        self.assertNotIn(first_item.movie.id, {candidate.movie.id for candidate in self.repository.active_candidates()})

    def test_maybe_later_marks_item_without_deactivating_candidate_pool_row(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]

        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.MAYBE_LATER),
        )

        self.assertEqual(RecommendationProcessingStatus.MAYBE_LATER, first_item.processing_status)
        self.assertIn(first_item.movie.id, self.repository.candidate_pool)

    def test_clear_not_interested_makes_historical_negative_no_longer_effective(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]

        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.NOT_INTERESTED),
        )
        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.CLEAR_NOT_INTERESTED),
        )

        self.assertEqual(
            [FeedbackType.NOT_INTERESTED, FeedbackType.CLEAR_NOT_INTERESTED],
            [feedback.feedback_type for feedback in self.repository.feedback[-2:]],
        )
        self.assertIn(first_item.movie.id, self.repository.candidate_pool)
        self.assertIn(first_item.movie.id, {candidate.movie.id for candidate in self.repository.active_candidates()})

    def test_not_interested_response_returns_current_effective_items_sorted_and_paged(self) -> None:
        session = self.service.recommend("hybrid")
        for item in session.items[:3]:
            self.service.submit_feedback(
                session.id,
                item.id,
                FeedbackRequest(feedback_type=FeedbackType.NOT_INTERESTED),
            )
        self.repository.feedback[-3].created_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self.repository.feedback[-2].created_at = datetime(2026, 5, 2, tzinfo=timezone.utc)
        self.repository.feedback[-1].created_at = datetime(2026, 5, 3, tzinfo=timezone.utc)
        self.service.submit_feedback(
            session.id,
            session.items[1].id,
            FeedbackRequest(feedback_type=FeedbackType.CLEAR_NOT_INTERESTED),
        )

        response = self.service.to_not_interested_response(limit=1, offset=0)

        self.assertEqual(1, response["limit"])
        self.assertEqual(0, response["offset"])
        self.assertEqual(2, response["total"])
        self.assertEqual(1, len(response["items"]))
        self.assertEqual(session.items[2].movie.id, response["items"][0]["movie_id"])

    def test_clear_not_interested_endpoint_semantics_appends_clear_and_restores_candidate_pool(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]
        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.NOT_INTERESTED),
        )

        cleared = self.service.clear_not_interested(first_item.movie.id)

        self.assertEqual(first_item.movie.id, cleared.movie.id)
        self.assertEqual(FeedbackType.CLEAR_NOT_INTERESTED, self.repository.feedback[-1].feedback_type)
        self.assertEqual(first_item.id, self.repository.feedback[-1].item_id)
        self.assertIn(first_item.movie.id, self.repository.candidate_pool)
        self.assertEqual([], self.service.to_not_interested_response()["items"])

    def test_record_watched_closes_wishlist_and_creates_history(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]
        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.WANT_TO_WATCH),
        )
        wishlist_item = next(iter(self.repository.wishlist.values()))

        history = self.service.record_watched(
            wishlist_item.id,
            RecordWatchedRequest(
                watched_date=date(2026, 5, 12),
                user_rating=4.5,
                quality="1080p",
                comment="worth watching",
            ),
        )

        self.assertEqual(WishlistStatus.WATCHED, wishlist_item.status)
        self.assertEqual(first_item.movie.id, history.movie_id)
        self.assertIn(history, self.repository.history)
        self.assertNotIn(first_item.movie.id, self.repository.candidate_pool)

    def test_wishlist_response_returns_active_items_sorted_newest_first_and_paged(self) -> None:
        session = self.service.recommend("hybrid")
        for item in session.items[:3]:
            self.service.submit_feedback(
                session.id,
                item.id,
                FeedbackRequest(feedback_type=FeedbackType.WANT_TO_WATCH),
        )
        wishlist_items = list(self.repository.wishlist.values())
        wishlist_items[0].created_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
        wishlist_items[1].created_at = datetime(2026, 5, 2, tzinfo=timezone.utc)
        wishlist_items[2].created_at = datetime(2026, 5, 3, tzinfo=timezone.utc)
        wishlist_items[1].status = WishlistStatus.REMOVED

        response = self.service.to_wishlist_response(limit=1, offset=0)

        self.assertEqual(1, response["limit"])
        self.assertEqual(0, response["offset"])
        self.assertEqual(2, response["total"])
        self.assertEqual(1, len(response["items"]))
        self.assertEqual(wishlist_items[2].id, response["items"][0]["id"])

    def test_wishlist_response_carries_originating_recommendation_display_fields(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]
        first_item.source_ref = "recommended_from:1292434"
        first_item.source_label = "Recommend from Yi Yi"
        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.WANT_TO_WATCH),
        )

        response = self.service.to_wishlist_response()

        self.assertEqual(first_item.score, response["items"][0]["score"])
        self.assertEqual("recommended_from:1292434", response["items"][0]["source_ref"])
        self.assertEqual("Recommend from Yi Yi", response["items"][0]["source_label"])

    def test_wishlist_response_without_recommendation_context_uses_null_display_fields(self) -> None:
        movie = next(iter(self.repository.movies_by_id.values()))
        wishlist_item = self.repository.add_to_wishlist(movie, "missing-session")

        response = self.service.to_wishlist_item_response(wishlist_item)

        self.assertIsNone(response["score"])
        self.assertIsNone(response["source_ref"])
        self.assertIsNone(response["source_label"])

    def test_remove_from_wishlist_closes_item_and_appends_removed_feedback(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]
        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.WANT_TO_WATCH),
        )
        wishlist_item = next(iter(self.repository.wishlist.values()))

        removed = self.service.remove_from_wishlist(wishlist_item.id)

        self.assertIs(removed, wishlist_item)
        self.assertEqual(WishlistStatus.REMOVED, wishlist_item.status)
        self.assertIsNotNone(wishlist_item.closed_at)
        self.assertEqual(FeedbackType.REMOVED_FROM_WISHLIST, self.repository.feedback[-1].feedback_type)
        self.assertEqual(first_item.id, self.repository.feedback[-1].item_id)
        self.assertEqual([], self.service.to_wishlist_response()["items"])

    def test_mark_watched_from_recommendation_marks_item_and_deactivates_candidate_pool_row(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]

        processed = self.service.mark_watched_from_recommendation(
            session.id,
            first_item.id,
            first_item.movie.id,
        )

        self.assertIs(processed, first_item)
        self.assertEqual(RecommendationProcessingStatus.WATCHED, first_item.processing_status)
        self.assertIsNotNone(first_item.processed_at)
        self.assertNotIn(first_item.movie.id, self.repository.candidate_pool)

    def test_mark_wishlist_item_watched_from_record_closes_active_wishlist_item(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]
        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.WANT_TO_WATCH),
        )
        wishlist_item = next(iter(self.repository.wishlist.values()))

        watched = self.service.mark_wishlist_item_watched_from_record(wishlist_item.id, first_item.movie.id)

        self.assertIs(watched, wishlist_item)
        self.assertEqual(WishlistStatus.WATCHED, wishlist_item.status)
        self.assertIsNotNone(wishlist_item.closed_at)
        self.assertNotIn(first_item.movie.id, self.repository.candidate_pool)
        self.assertEqual([], self.service.to_wishlist_response()["items"])

    def test_mark_watched_from_recommendation_rejects_movie_mismatch(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]

        with self.assertRaisesRegex(ValueError, "does not match"):
            self.service.mark_watched_from_recommendation(session.id, first_item.id, "different-movie")

    def test_create_recommendation_service_defaults_to_in_memory_repository(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            service = create_recommendation_service()

        self.assertIsInstance(service.repository, InMemoryMovieRepository)

    def test_postgres_repository_maps_database_movie_rows_to_domain_movies(self) -> None:
        repository = PostgresRecommendationRepository.__new__(PostgresRecommendationRepository)

        movie = repository._movie_from_row(
            {
                "id": "movie-1",
                "douban_subject_id": "1292052",
                "douban_url": None,
                "title": "The Shawshank Redemption",
                "year": 1994,
                "directors": ["Frank Darabont"],
                "actors": '["Tim Robbins", "Morgan Freeman"]',
                "genres": ["Drama"],
                "countries": ["United States"],
                "douban_rating": "9.7",
                "douban_vote_count": "3100000",
                "poster_url": "https://img.example/poster.webp",
            }
        )

        self.assertEqual("movie-1", movie.id)
        self.assertEqual("The Shawshank Redemption", movie.title)
        self.assertEqual(("Frank Darabont",), movie.directors)
        self.assertEqual(("Tim Robbins", "Morgan Freeman"), movie.actors)
        self.assertEqual("https://movie.douban.com/subject/1292052/", movie.douban_url)
        self.assertEqual("https://img.example/poster.webp", movie.poster_url)

    def test_postgres_refresh_parameterizes_recommended_from_like_pattern(self) -> None:
        repository = PostgresRecommendationRepository.__new__(PostgresRecommendationRepository)
        repository.connection = _FakeRefreshConnection()
        repository.sessions = {}
        repository.feedback = []
        repository.wishlist = {}
        repository.not_interested = {}

        repository.refresh()

        candidate_query, candidate_params = repository.connection.calls[2]
        self.assertIn("cp.source_ref LIKE %s", candidate_query)
        self.assertNotIn("LIKE 'recommended_from:%'", candidate_query)
        self.assertEqual(("recommended_from:%", date.today().year), candidate_params)


def _many_movies(count: int) -> list[Movie]:
    return [
        Movie(
            id=f"m-{index}",
            title=f"Movie {index}",
            year=1990 + index,
            directors=(f"Director {index % 4}",),
            actors=(f"Actor {index}",),
            genres=("Drama",),
            countries=("Japan",),
            douban_rating=9.5 - index * 0.1,
            douban_vote_count=100000 - index,
            douban_url=f"https://movie.douban.com/subject/{1000000 + index}/",
        )
        for index in range(count)
    ]


def _seed_bandit_training_history(repository: InMemoryMovieRepository, count: int) -> None:
    for index, movie in enumerate(list(repository.movies_by_id.values())[:count], start=1):
        item = RecommendationItem(
            movie=movie,
            rank=1,
            slot_type=SlotType.EXPLORE,
            score=float(index),
            score_components={
                "total": float(index),
                "personal_preference": float(index) / 10.0,
                "public_quality": movie.douban_rating,
                "novelty": 0.0,
            },
            id=f"training-item-{index}",
        )
        session = RecommendationSession(
            strategy="hybrid",
            items=[item],
            id=f"training-session-{index}",
        )
        repository.sessions[session.id] = session
        repository.feedback.append(
            Feedback(
                session_id=session.id,
                item_id=item.id,
                movie_id=movie.id,
                feedback_type=FeedbackType.MAYBE_LATER,
                feedback_value=0.2,
            )
        )


class _FakeRefreshConnection:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return _FakeCursor([])


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


if __name__ == "__main__":
    unittest.main()


