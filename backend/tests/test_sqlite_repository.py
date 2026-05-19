import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail


class SQLiteViewingHistoryRepositoryTest(unittest.TestCase):
    def test_persists_confirmed_viewing_history_without_raw_history_table(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                result = repository.persist_confirmed_viewing_history(
                    _confirmed(source_row_hash="hash-1"),
                    _detail(),
                )

                movie = repository.connection.execute("SELECT * FROM movies").fetchone()
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()
                raw_table = repository.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'viewing_history_raw'"
                ).fetchone()

        self.assertEqual("1291992", result.movie.douban_subject_id)
        self.assertEqual("1291992", movie["douban_subject_id"])
        self.assertEqual("末路狂花 Thelma & Louise", movie["title"])
        self.assertEqual(["Ridley Scott"], json.loads(movie["directors_json"]))
        self.assertEqual(["剧情", "惊悚", "犯罪"], json.loads(movie["genres_json"]))
        self.assertEqual(9.0, movie["douban_rating"])
        self.assertEqual(353101, movie["douban_vote_count"])
        self.assertEqual(result.movie.id, history["movie_id"])
        self.assertEqual("2026-03-19", history["watched_date"])
        self.assertEqual(4.2, history["user_rating"])
        self.assertEqual("1080p", history["quality"])
        self.assertEqual("test comment", history["comment"])
        self.assertEqual("hash-1", history["source_row_hash"])
        self.assertEqual("MOVIES.xlsx#2026", history["source_file"])
        self.assertIsNone(raw_table)

    def test_reimport_same_source_row_hash_updates_existing_history(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                first = repository.persist_confirmed_viewing_history(
                    _confirmed(source_row_hash="hash-1", rating=4.0, comment="first"),
                    _detail(rating=8.9),
                )
                second = repository.persist_confirmed_viewing_history(
                    _confirmed(source_row_hash="hash-1", rating=4.5, comment="updated"),
                    _detail(rating=9.0),
                )

                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]
                movie = repository.connection.execute("SELECT * FROM movies").fetchone()
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(first.movie.id, second.movie.id)
        self.assertEqual(first.history.id, second.history.id)
        self.assertEqual(1, movie_count)
        self.assertEqual(1, history_count)
        self.assertEqual(9.0, movie["douban_rating"])
        self.assertEqual(4.5, history["user_rating"])
        self.assertEqual("updated", history["comment"])

    def test_rejects_history_without_source_row_hash(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                with self.assertRaisesRegex(ValueError, "source_row_hash"):
                    repository.persist_confirmed_viewing_history(
                        _confirmed(source_row_hash=None),
                        _detail(),
                    )

    def test_requires_matching_confirmed_subject_and_detail_subject(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                with self.assertRaisesRegex(ValueError, "does not match"):
                    repository.persist_confirmed_viewing_history(
                        _confirmed(subject_id="1291992", source_row_hash="hash-1"),
                        _detail(subject_id="wrong"),
                    )


def _confirmed(
    subject_id: str = "1291992",
    source_row_hash: str | None = "hash-1",
    rating: float = 4.2,
    comment: str = "test comment",
) -> ConfirmedViewingHistoryInput:
    return ConfirmedViewingHistoryInput(
        source_raw_id="raw-1",
        source_file="MOVIES.xlsx#2026",
        source_row_number=8,
        douban_subject_id=subject_id,
        watched_date=date(2026, 3, 19),
        user_rating=rating,
        source_row_hash=source_row_hash,
        quality="1080p",
        comment=comment,
    )


def _detail(subject_id: str = "1291992", rating: float = 9.0) -> DoubanMovieDetail:
    return DoubanMovieDetail(
        subject_id=subject_id,
        title="末路狂花 Thelma & Louise",
        year=1991,
        directors=("Ridley Scott",),
        actors=("Geena Davis", "Susan Sarandon"),
        genres=("剧情", "惊悚", "犯罪"),
        countries=("美国",),
        douban_rating=rating,
        douban_vote_count=353101,
        summary="A road movie.",
        poster_url="https://img.example/poster.webp",
        url=f"https://movie.douban.com/subject/{subject_id}/",
    )


if __name__ == "__main__":
    unittest.main()
