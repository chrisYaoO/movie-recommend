import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail
from backend.app.services.google_sheets_service import AppendSheetRowResult
from backend.app.services.metadata_service import FakeDoubanDetailAdapter
from backend.app.services.viewing_history_record_service import (
    RecordViewingHistoryRequest,
    ViewingHistoryRecordService,
)


class ViewingHistoryRecordServiceTest(unittest.TestCase):
    def test_appends_sheet_row_then_fetches_missing_movie_and_persists_history(self) -> None:
        detail = DoubanMovieDetail(
            subject_id="2222996",
            title="Still Walking",
            year=2008,
            directors=("Hirokazu Kore-eda",),
            actors=("Hiroshi Abe",),
            genres=("Drama",),
            countries=("Japan",),
            douban_rating=8.8,
            douban_vote_count=300000,
            url="https://movie.douban.com/subject/2222996/",
        )
        sheets = _FakeSheets()

        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                detail_adapter = FakeDoubanDetailAdapter({"2222996": detail})
                service = ViewingHistoryRecordService(
                    repository=repository,
                    detail_adapter=detail_adapter,
                    sheets=sheets,
                )

                result = service.record(
                    RecordViewingHistoryRequest(
                        douban_subject_id="2222996",
                        title="Still Walking",
                        director="Hirokazu Kore-eda",
                        year=2008,
                        watched_date=date(2026, 5, 26),
                        rating=4.5,
                        quality="1080p",
                        comment="quietly great",
                        sheet="2026",
                    )
                )
                movie = repository.connection.execute("SELECT * FROM movies").fetchone()
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual("2222996", result.douban_subject_id)
        self.assertIsNotNone(result.movie_id)
        self.assertEqual("Still Walking", result.title)
        self.assertTrue(result.fetched_movie_detail)
        self.assertEqual(0, result.recommendation_inserted_count)
        self.assertEqual("2026", result.source_sheet_name)
        self.assertEqual(27, result.source_row_number)
        self.assertEqual("2026!A27:I27", result.sheet_updated_range)
        self.assertEqual("2222996", movie["douban_subject_id"])
        self.assertEqual(movie["id"], history["movie_id"])
        self.assertEqual("2222996", history["douban_subject_id"])
        self.assertEqual(4.5, history["user_rating"])
        self.assertEqual("quietly great", history["comment"])
        self.assertEqual(["2222996"], detail_adapter.fetches)
        self.assertEqual(
            [
                "2026-05-26",
                "Still Walking",
                "Hirokazu Kore-eda",
                2008,
                4.5,
                "1080p",
                "quietly great",
                "2222996",
                "",
            ],
            sheets.rows[0],
        )

    def test_recording_existing_movie_backfills_movie_id_immediately(self) -> None:
        detail = DoubanMovieDetail(
            subject_id="2222996",
            title="Still Walking",
            year=2008,
            directors=("Hirokazu Kore-eda",),
            url="https://movie.douban.com/subject/2222996/",
        )
        sheets = _FakeSheets()

        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                existing = repository.upsert_movie_detail(detail)
                detail_adapter = FakeDoubanDetailAdapter({"2222996": detail})
                service = ViewingHistoryRecordService(
                    repository=repository,
                    detail_adapter=detail_adapter,
                    sheets=sheets,
                )

                result = service.record(
                    RecordViewingHistoryRequest(
                        douban_subject_id="2222996",
                        watched_date=date(2026, 5, 26),
                        rating=4.5,
                        sheet="2026",
                    )
                )
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(existing.id, result.movie_id)
        self.assertEqual("Still Walking", result.title)
        self.assertFalse(result.fetched_movie_detail)
        self.assertEqual(0, result.recommendation_inserted_count)
        self.assertEqual(existing.id, history["movie_id"])
        self.assertEqual([], detail_adapter.fetches)


class _FakeSheets:
    def __init__(self):
        self.rows = []

    def append_viewing_history_row(self, sheet_name, values):
        self.rows.append(values)
        return AppendSheetRowResult(
            sheet_name=sheet_name,
            row_number=27,
            updated_range=f"{sheet_name}!A27:I27",
        )


if __name__ == "__main__":
    unittest.main()


