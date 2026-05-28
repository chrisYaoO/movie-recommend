import unittest
from datetime import date
from unittest.mock import patch

from backend.app.api.routes import record_viewing_history, search_movies
from backend.app.services.movie_search_service import MovieSearchCandidate
from backend.app.services.viewing_history_record_service import RecordViewingHistoryRequest, RecordViewingHistoryResult


class ApiRoutesTest(unittest.TestCase):
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
        request = RecordViewingHistoryRequest(
            douban_subject_id="2222996",
            watched_date=date(2026, 5, 26),
            rating=4.5,
            quality="1080p",
            comment="quietly great",
            sheet="2026",
        )

        with patch("backend.app.api.routes.get_viewing_history_record_service", return_value=fake_service):
            response = record_viewing_history(request)

        self.assertEqual("2222996", response["douban_subject_id"])
        self.assertEqual("2026", response["source_sheet_name"])
        self.assertEqual([request], fake_service.requests)


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


if __name__ == "__main__":
    unittest.main()


