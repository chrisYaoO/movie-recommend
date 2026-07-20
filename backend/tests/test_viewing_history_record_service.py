import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail
from backend.app.services.google_sheets_service import SheetHistoryLocation
from backend.app.services.metadata_service import FakeDoubanDetailAdapter
from backend.app.services.viewing_history_sync_service import ViewingHistorySyncService
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
            poster_url="https://img.example/p123456.webp",
        )
        sheets = _FakeSheets()

        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                detail_adapter = FakeDoubanDetailAdapter({"2222996": detail})
                service = ViewingHistoryRecordService(
                    repository=repository,
                    detail_adapter=detail_adapter,
                    syncer=ViewingHistorySyncService(repository, sheets),
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
        self.assertEqual("2026!A27:J27", result.sheet_updated_range)
        self.assertEqual("synced", result.sync_state)
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
                "123456",
            ],
            sheets.rows[0],
        )

    def test_recording_existing_movie_uses_local_metadata_for_sheet_row(self) -> None:
        detail = DoubanMovieDetail(
            subject_id="2222996",
            title="Still Walking",
            year=2008,
            directors=("是枝裕和 Hirokazu Kore-eda",),
            poster_url="https://img.example/p456789.webp",
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
                    syncer=ViewingHistorySyncService(repository, sheets),
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
        self.assertEqual(
            [
                "2026-05-26",
                "Still Walking",
                "是枝裕和",
                2008,
                4.5,
                "",
                "",
                "2222996",
                "456789",
            ],
            sheets.rows[0],
        )

    def test_sheet_failure_does_not_roll_back_local_history(self) -> None:
        detail = DoubanMovieDetail(subject_id="2222996", title="Still Walking", year=2008)
        sheets = _FakeSheets(error=TimeoutError("offline"))

        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                service = ViewingHistoryRecordService(
                    repository,
                    FakeDoubanDetailAdapter({"2222996": detail}),
                    ViewingHistorySyncService(repository, sheets),
                )

                result = service.record(
                    RecordViewingHistoryRequest(
                        douban_subject_id="2222996",
                        watched_date=date(2026, 5, 26),
                        rating=4.5,
                        sheet="2026",
                    )
                )
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]
                task = repository.find_pending_sheet_sync_tasks()[0]

        self.assertEqual("failed", result.sync_state)
        self.assertEqual(1, history_count)
        self.assertEqual(1, task.attempts)


class _FakeSheets:
    def __init__(self, error=None):
        self.rows = []
        self.error = error

    def upsert_history_row(self, projection, hinted_sheet_name, hinted_row_number):
        self.rows.append(projection.values)
        if self.error:
            raise self.error
        return SheetHistoryLocation(
            sheet_name=projection.sheet_name,
            row_number=27,
            updated_range=f"{projection.sheet_name}!A27:J27",
        )

    def delete_history_row(self, history_id, hinted_sheet_name, hinted_row_number):
        return True


if __name__ == "__main__":
    unittest.main()


