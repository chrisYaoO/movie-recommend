import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

import backend.app.api.routes as routes
from backend.app.api.routes import (
    clear_not_interested,
    close_viewing_history_record_service,
    get_not_interested,
    get_recommendations,
    get_recommendation_session,
    get_wishlist,
    prewarm_viewing_history_record_service,
    record_viewing_history,
    remove_from_wishlist,
    search_movies,
    should_prewarm_record_selenium,
    undo_recommendation_item_processing,
)
from backend.app.models.domain import RecommendationProcessingStatus, WishlistStatus
from backend.app.services.metadata_service import DoubanSeleniumDetailAdapter
from backend.app.services.movie_search_service import MovieSearchCandidate
from backend.app.services.viewing_history_record_service import RecordViewingHistoryRequest, RecordViewingHistoryResult


class ApiRoutesTest(unittest.TestCase):
    def test_record_detail_adapter_uses_selenium(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            adapter = routes.create_record_detail_adapter()

        try:
            self.assertIsInstance(adapter, DoubanSeleniumDetailAdapter)
        finally:
            adapter.close()

    def test_desktop_mode_prewarms_record_selenium_by_default(self) -> None:
        with patch.dict("os.environ", {"MOVIES_DESKTOP": "1"}, clear=True):
            self.assertTrue(should_prewarm_record_selenium())

    def test_record_selenium_prewarm_can_be_disabled(self) -> None:
        with patch.dict(
            "os.environ",
            {"MOVIES_DESKTOP": "1", "MOVIES_PREWARM_RECORD_SELENIUM": "0"},
            clear=True,
        ):
            self.assertFalse(should_prewarm_record_selenium())

    def test_prewarm_initializes_the_shared_record_service_adapter(self) -> None:
        fake_service = SimpleNamespace(detail_adapter=SimpleNamespace(prewarm=Mock()))

        with patch("backend.app.api.routes.get_viewing_history_record_service", return_value=fake_service):
            prewarm_viewing_history_record_service()

        fake_service.detail_adapter.prewarm.assert_called_once_with()

    def test_close_record_service_closes_adapter_and_repository(self) -> None:
        fake_service = SimpleNamespace(
            detail_adapter=SimpleNamespace(close=Mock()),
            repository=SimpleNamespace(close=Mock()),
        )

        with patch("backend.app.api.routes.viewing_history_record_service", fake_service):
            close_viewing_history_record_service()

        fake_service.detail_adapter.close.assert_called_once_with()
        fake_service.repository.close.assert_called_once_with()

    def test_search_movies_returns_candidates(self) -> None:
        fake_service = _FakeMovieSearchService(
            [
                MovieSearchCandidate(
                    subject_id="2222996",
                    title="Still Walking",
                    year=2008,
                    director="Hirokazu Kore-eda",
                    url="https://movie.douban.com/subject/2222996/",
                )
            ]
        )

        with patch("backend.app.api.routes.movie_search_service", fake_service):
            response = search_movies(q="Still Walking")

        self.assertEqual(
            {
                "query": "Still Walking",
                "items": [
                    {
                        "subject_id": "2222996",
                        "title": "Still Walking",
                        "year": 2008,
                        "director": "Hirokazu Kore-eda",
                        "url": "https://movie.douban.com/subject/2222996/",
                    }
                ],
            },
            response,
        )
        self.assertEqual(["Still Walking"], fake_service.queries)

    def test_record_viewing_history_uses_record_service(self) -> None:
        fake_service = _FakeRecordService()
        fake_recommendation_service = _FakeRecommendationService()
        request = RecordViewingHistoryRequest(
            douban_subject_id="2222996",
            watched_date=date(2026, 5, 26),
            rating=4.5,
            quality="1080p",
            comment="quietly great",
            sheet="2026",
        )

        with (
            patch("backend.app.api.routes.get_viewing_history_record_service", return_value=fake_service),
            patch("backend.app.api.routes.service", fake_recommendation_service),
        ):
            response = record_viewing_history(request)

        self.assertEqual("2222996", response["douban_subject_id"])
        self.assertEqual("2026", response["source_sheet_name"])
        self.assertEqual([request], fake_service.requests)
        self.assertEqual(["movie-id"], fake_recommendation_service.watched_movie_ids)

    def test_record_viewing_history_marks_originating_recommendation_item_processed(self) -> None:
        fake_service = _FakeRecordService()
        fake_recommendation_service = _FakeRecommendationService()
        request = RecordViewingHistoryRequest(
            douban_subject_id="2222996",
            watched_date=date(2026, 5, 26),
            rating=4.5,
            quality="1080p",
            comment="quietly great",
            sheet="2026",
            session_id="session-1",
            recommendation_item_id="item-1",
        )

        with (
            patch("backend.app.api.routes.get_viewing_history_record_service", return_value=fake_service),
            patch("backend.app.api.routes.service", fake_recommendation_service),
        ):
            response = record_viewing_history(request)

        self.assertEqual("session-1", response["session_id"])
        self.assertEqual("item-1", response["recommendation_item_id"])
        self.assertEqual("watched", response["processing_status"])
        self.assertIsNotNone(response["processed_at"])
        self.assertEqual([("session-1", "item-1", "movie-id")], fake_recommendation_service.watched_recommendations)

    def test_record_viewing_history_marks_originating_wishlist_item_watched(self) -> None:
        fake_service = _FakeRecordService()
        fake_recommendation_service = _FakeRecommendationService()
        request = RecordViewingHistoryRequest(
            douban_subject_id="2222996",
            watched_date=date(2026, 5, 26),
            rating=4.5,
            quality="1080p",
            comment="quietly great",
            sheet="2026",
            wishlist_id="wishlist-1",
        )

        with (
            patch("backend.app.api.routes.get_viewing_history_record_service", return_value=fake_service),
            patch("backend.app.api.routes.service", fake_recommendation_service),
        ):
            response = record_viewing_history(request)

        self.assertEqual("wishlist-1", response["wishlist_id"])
        self.assertEqual("watched", response["wishlist_status"])
        self.assertEqual([("wishlist-1", "movie-id")], fake_recommendation_service.watched_wishlist_items)

    def test_get_recommendation_session_returns_existing_session(self) -> None:
        fake_service = _FakeRecommendationService()

        with patch("backend.app.api.routes.service", fake_service):
            response = get_recommendation_session("session-1")

        self.assertEqual({"id": "session-1", "items": []}, response)
        self.assertEqual(["session-1"], fake_service.session_ids)

    def test_get_recommendations_passes_seed_and_cooldown_to_service(self) -> None:
        fake_service = _FakeRecommendationService()

        with patch("backend.app.api.routes.service", fake_service):
            response = get_recommendations(strategy="hybrid", seed=7, exposure_cooldown_sessions=1)

        self.assertEqual({"id": "new-session", "items": []}, response)
        self.assertEqual([("hybrid", 7, 1)], fake_service.recommendation_requests)

    def test_get_wishlist_passes_pagination_to_service(self) -> None:
        fake_service = _FakeRecommendationService()

        with patch("backend.app.api.routes.service", fake_service):
            response = get_wishlist(limit=10, offset=20)

        self.assertEqual({"limit": 10, "offset": 20, "items": []}, response)
        self.assertEqual([(10, 20)], fake_service.wishlist_pages)

    def test_remove_from_wishlist_uses_service(self) -> None:
        fake_service = _FakeRecommendationService()

        with patch("backend.app.api.routes.service", fake_service):
            response = remove_from_wishlist("wishlist-1")

        self.assertEqual({"id": "wishlist-1", "status": "removed"}, response)
        self.assertEqual(["wishlist-1"], fake_service.removed_wishlist_ids)

    def test_get_not_interested_passes_pagination_to_service(self) -> None:
        fake_service = _FakeRecommendationService()

        with patch("backend.app.api.routes.service", fake_service):
            response = get_not_interested(limit=10, offset=20)

        self.assertEqual({"limit": 10, "offset": 20, "items": []}, response)
        self.assertEqual([(10, 20)], fake_service.not_interested_pages)

    def test_clear_not_interested_uses_service(self) -> None:
        fake_service = _FakeRecommendationService()

        with patch("backend.app.api.routes.service", fake_service):
            response = clear_not_interested("movie-1")

        self.assertEqual({"movie_id": "movie-1", "state": "not_interested"}, response)
        self.assertEqual(["movie-1"], fake_service.cleared_not_interested_movie_ids)

    def test_undo_recommendation_item_processing_uses_service(self) -> None:
        fake_service = _FakeRecommendationService()

        with patch("backend.app.api.routes.service", fake_service):
            response = undo_recommendation_item_processing("session-1", "item-1")

        self.assertEqual({"id": "item-1", "processing_status": None}, response)
        self.assertEqual([("session-1", "item-1")], fake_service.undone_recommendation_items)


class _FakeMovieSearchService:
    def __init__(self, candidates):
        self.candidates = candidates
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return self.candidates

    def to_response(self, query, candidates):
        return {
            "query": query,
            "items": [
                {
                    "subject_id": candidate.subject_id,
                    "title": candidate.title,
                    "year": candidate.year,
                    "director": candidate.director,
                    "url": candidate.url,
                }
                for candidate in candidates
            ],
        }


class _FakeRecordService:
    def __init__(self):
        self.requests = []

    def record(self, request):
        self.requests.append(request)
        return RecordViewingHistoryResult(
            movie_id="movie-id",
            viewing_history_id="history-id",
            douban_subject_id=request.douban_subject_id,
            title="Still Walking",
            source_sheet_name=request.sheet,
            source_row_number=27,
            source_row_checksum="hash",
            sheet_updated_range=f"{request.sheet}!A27:I27",
            fetched_movie_detail=True,
            recommendation_inserted_count=0,
        )

    def to_response(self, result):
        return {
            "movie_id": result.movie_id,
            "viewing_history_id": result.viewing_history_id,
            "douban_subject_id": result.douban_subject_id,
            "title": result.title,
            "source_sheet_name": result.source_sheet_name,
            "source_row_number": result.source_row_number,
            "source_row_checksum": result.source_row_checksum,
            "sheet_updated_range": result.sheet_updated_range,
            "fetched_movie_detail": result.fetched_movie_detail,
            "recommendation_inserted_count": result.recommendation_inserted_count,
        }


class _FakeRecommendationService:
    def __init__(self):
        self.session_ids = []
        self.watched_movie_ids = []
        self.watched_recommendations = []
        self.watched_wishlist_items = []
        self.wishlist_pages = []
        self.removed_wishlist_ids = []
        self.not_interested_pages = []
        self.cleared_not_interested_movie_ids = []
        self.recommendation_requests = []
        self.undone_recommendation_items = []

    def recommend(self, strategy="hybrid", explore_seed=None, exposure_cooldown_sessions=5):
        self.recommendation_requests.append((strategy, explore_seed, exposure_cooldown_sessions))
        return {"id": "new-session"}

    def get_session(self, session_id):
        self.session_ids.append(session_id)
        return {"id": session_id}

    def to_session_response(self, session):
        return {"id": session["id"], "items": []}

    def to_wishlist_response(self, limit=10, offset=0):
        self.wishlist_pages.append((limit, offset))
        return {"limit": limit, "offset": offset, "items": []}

    def mark_watched_movie(self, movie_id):
        self.watched_movie_ids.append(movie_id)

    def mark_watched_from_recommendation(self, session_id, item_id, movie_id):
        self.watched_recommendations.append((session_id, item_id, movie_id))
        return SimpleNamespace(
            processing_status=RecommendationProcessingStatus.WATCHED,
            processed_at=date(2026, 5, 26),
        )

    def remove_from_wishlist(self, wishlist_id):
        self.removed_wishlist_ids.append(wishlist_id)
        return {"id": wishlist_id, "status": "removed"}

    def mark_wishlist_item_watched_from_record(self, wishlist_id, movie_id):
        self.watched_wishlist_items.append((wishlist_id, movie_id))
        return SimpleNamespace(status=WishlistStatus.WATCHED)

    def to_wishlist_item_response(self, item):
        return item

    def to_not_interested_response(self, limit=10, offset=0):
        self.not_interested_pages.append((limit, offset))
        return {"limit": limit, "offset": offset, "items": []}

    def clear_not_interested(self, movie_id):
        self.cleared_not_interested_movie_ids.append(movie_id)
        return {"movie_id": movie_id}

    def to_not_interested_item_response(self, item):
        return {"movie_id": item["movie_id"], "state": "not_interested"}

    def undo_recommendation_item_processing(self, session_id, item_id):
        self.undone_recommendation_items.append((session_id, item_id))
        return {"id": item_id, "processing_status": None}

    def to_recommendation_item_response(self, item):
        return item


if __name__ == "__main__":
    unittest.main()


