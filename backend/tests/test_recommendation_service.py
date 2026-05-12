from datetime import date
import unittest

from backend.app.models.domain import FeedbackType, SlotType, WishlistStatus
from backend.app.services.recommendation_service import (
    FeedbackRequest,
    InMemoryMovieRepository,
    RecommendationService,
    RecordWatchedRequest,
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


if __name__ == "__main__":
    unittest.main()
