from datetime import date
import os
import unittest
from unittest.mock import patch

from backend.app.models.domain import FeedbackType, SlotType, WishlistStatus
from backend.app.services.recommendation_service import (
    FeedbackRequest,
    InMemoryMovieRepository,
    PostgresRecommendationRepository,
    RecommendationService,
    RecordWatchedRequest,
    create_recommendation_service,
)


class RecommendationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryMovieRepository()
        self.service = RecommendationService(self.repository)

    def test_hybrid_recommendation_returns_three_exploit_and_two_explore(self) -> None:
        session = self.service.recommend("hybrid")

        self.assertEqual(5, len(session.items))
        self.assertEqual([1, 2, 3, 4, 5], [item.rank for item in session.items])
        self.assertEqual(3, sum(1 for item in session.items if item.slot_type == SlotType.EXPLOIT))
        self.assertEqual(2, sum(1 for item in session.items if item.slot_type == SlotType.EXPLORE))
        self.assertEqual(5, len({item.movie.id for item in session.items}))

    def test_explore_seed_makes_explore_slots_reproducible_without_changing_exploit_slots(self) -> None:
        first = self.service.recommend("hybrid", explore_seed=1)
        repeated = self.service.recommend("hybrid", explore_seed=1)
        different = self.service.recommend("hybrid", explore_seed=2)

        self.assertEqual(
            [item.movie.id for item in first.items],
            [item.movie.id for item in repeated.items],
        )
        self.assertEqual(
            [item.movie.id for item in first.items[:3]],
            [item.movie.id for item in different.items[:3]],
        )
        self.assertNotEqual(
            [item.movie.id for item in first.items[3:]],
            [item.movie.id for item in different.items[3:]],
        )

    def test_want_to_watch_adds_wishlist_and_excludes_movie_from_future_sessions(self) -> None:
        session = self.service.recommend("hybrid")
        first_item = session.items[0]

        self.service.submit_feedback(
            session.id,
            first_item.id,
            FeedbackRequest(feedback_type=FeedbackType.WANT_TO_WATCH),
        )
        next_session = self.service.recommend("hybrid")

        active_wishlist = list(self.repository.wishlist.values())
        self.assertEqual(1, len(active_wishlist))
        self.assertEqual(first_item.movie.id, active_wishlist[0].movie.id)
        self.assertNotIn(first_item.movie.id, {item.movie.id for item in next_session.items})

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
            }
        )

        self.assertEqual("movie-1", movie.id)
        self.assertEqual("The Shawshank Redemption", movie.title)
        self.assertEqual(("Frank Darabont",), movie.directors)
        self.assertEqual(("Tim Robbins", "Morgan Freeman"), movie.actors)
        self.assertEqual("https://movie.douban.com/subject/1292052/", movie.douban_url)


if __name__ == "__main__":
    unittest.main()


