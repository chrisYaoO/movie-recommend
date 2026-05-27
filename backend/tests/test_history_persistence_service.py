import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail
from backend.app.services.history_persistence_service import persist_confirmed_viewing_history
from backend.app.services.metadata_service import FakeDoubanDetailAdapter


class HistoryPersistenceServiceTest(unittest.TestCase):
    def test_persists_existing_movie_without_fetching(self) -> None:
        with TemporaryDirectory() as directory:
            adapter = FakeDoubanDetailAdapter()

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                with repository.connection:
                    repository.upsert_movie_detail(_detail("1291992", "Thelma & Louise"))

                result = persist_confirmed_viewing_history(
                    [_confirmed("1291992", "hash-1")],
                    adapter,
                    repository,
                )

                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(1, result.persisted_count)
        self.assertEqual(1, result.existing_count)
        self.assertEqual(0, result.fetched_count)
        self.assertEqual(0, result.failed_count)
        self.assertEqual([], adapter.fetches)
        self.assertEqual(1, movie_count)
        self.assertEqual("hash-1", history["source_row_hash"])

    def test_fetches_missing_movie_then_persists_detail_and_history(self) -> None:
        with TemporaryDirectory() as directory:
            adapter = FakeDoubanDetailAdapter({"1291992": _detail("1291992", "Thelma & Louise")})

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                result = persist_confirmed_viewing_history(
                    [_confirmed("1291992", "hash-1")],
                    adapter,
                    repository,
                )

                movie = repository.connection.execute("SELECT * FROM movies").fetchone()
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(1, result.persisted_count)
        self.assertEqual(0, result.existing_count)
        self.assertEqual(1, result.fetched_count)
        self.assertEqual(["1291992"], adapter.fetches)
        self.assertEqual("1291992", movie["douban_subject_id"])
        self.assertEqual("hash-1", history["source_row_hash"])

    def test_continues_after_failed_detail_lookup(self) -> None:
        with TemporaryDirectory() as directory:
            adapter = FakeDoubanDetailAdapter({"ok": _detail("ok", "Ok Movie")})

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                result = persist_confirmed_viewing_history(
                    [
                        _confirmed("missing", "hash-missing"),
                        _confirmed("ok", "hash-ok"),
                    ],
                    adapter,
                    repository,
                )

                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]

        self.assertEqual(["missing", "ok"], adapter.fetches)
        self.assertEqual(1, result.persisted_count)
        self.assertEqual(1, result.fetched_count)
        self.assertEqual(1, result.failed_count)
        self.assertEqual("failed", result.items[0].status)
        self.assertIn("missing", result.items[0].error or "")
        self.assertEqual("fetched", result.items[1].status)
        self.assertEqual(1, movie_count)
        self.assertEqual(1, history_count)

    def test_reimport_same_source_row_hash_updates_history(self) -> None:
        with TemporaryDirectory() as directory:
            adapter = FakeDoubanDetailAdapter()

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                with repository.connection:
                    repository.upsert_movie_detail(_detail("1291992", "Thelma & Louise"))

                first = persist_confirmed_viewing_history(
                    [_confirmed("1291992", "hash-1", rating=4.0, comment="first")],
                    adapter,
                    repository,
                )
                second = persist_confirmed_viewing_history(
                    [_confirmed("1291992", "hash-1", rating=4.5, comment="updated")],
                    adapter,
                    repository,
                )

                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(first.items[0].viewing_history_id, second.items[0].viewing_history_id)
        self.assertEqual(1, history_count)
        self.assertEqual(4.5, history["user_rating"])
        self.assertEqual("updated", history["comment"])

    def test_reimport_same_source_row_with_changed_hash_updates_history(self) -> None:
        with TemporaryDirectory() as directory:
            adapter = FakeDoubanDetailAdapter()

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                with repository.connection:
                    repository.upsert_movie_detail(_detail("1291992", "Thelma & Louise"))

                first = persist_confirmed_viewing_history(
                    [_confirmed("1291992", "hash-before", rating=4.0, comment="first")],
                    adapter,
                    repository,
                )
                second = persist_confirmed_viewing_history(
                    [_confirmed("1291992", "hash-after", rating=4.5, comment="updated")],
                    adapter,
                    repository,
                )

                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(first.items[0].viewing_history_id, second.items[0].viewing_history_id)
        self.assertEqual(1, history_count)
        self.assertEqual("hash-after", history["source_row_hash"])
        self.assertEqual(4.5, history["user_rating"])
        self.assertEqual("updated", history["comment"])


def _confirmed(
    subject_id: str,
    source_row_hash: str,
    rating: float = 4.2,
    comment: str = "test comment",
) -> ConfirmedViewingHistoryInput:
    return ConfirmedViewingHistoryInput(
        source_raw_id=f"raw-{source_row_hash}",
        source_file="MOVIES.xlsx#2026",
        source_row_number=8,
        douban_subject_id=subject_id,
        watched_date=date(2026, 3, 19),
        user_rating=rating,
        source_row_hash=source_row_hash,
        quality="1080p",
        comment=comment,
    )


def _detail(subject_id: str, title: str) -> DoubanMovieDetail:
    return DoubanMovieDetail(
        subject_id=subject_id,
        title=title,
        year=1991,
        directors=("Ridley Scott",),
        actors=("Geena Davis",),
        genres=("剧情", "犯罪"),
        countries=("美国",),
        douban_rating=9.0,
        douban_vote_count=353101,
        url=f"https://movie.douban.com/subject/{subject_id}/",
    )


if __name__ == "__main__":
    unittest.main()
