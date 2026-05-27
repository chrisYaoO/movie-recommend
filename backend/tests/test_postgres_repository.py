import os
import unittest
from datetime import date

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail


@unittest.skipUnless(os.getenv("MOVIES_POSTGRES_DSN"), "MOVIES_POSTGRES_DSN is not configured")
class PostgresViewingHistoryRepositoryTest(unittest.TestCase):
    def test_persists_and_updates_confirmed_viewing_history(self) -> None:
        dsn = os.environ["MOVIES_POSTGRES_DSN"]
        with PostgresViewingHistoryRepository(dsn) as repository:
            _reset_schema(repository)
            repository.initialize_schema()

            first = repository.persist_confirmed_viewing_history(
                _confirmed(source_row_hash="hash-1", rating=4.0, comment="first"),
                _detail(rating=8.9),
            )
            second = repository.persist_confirmed_viewing_history(
                _confirmed(source_row_hash="hash-1", rating=4.5, comment="updated"),
                _detail(rating=9.0),
            )

            movie_count = repository.connection.execute("SELECT COUNT(*) AS count FROM movies").fetchone()["count"]
            history_count = repository.connection.execute("SELECT COUNT(*) AS count FROM viewing_history").fetchone()[
                "count"
            ]
            movie = repository.connection.execute("SELECT * FROM movies").fetchone()
            history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(first.movie.id, second.movie.id)
        self.assertEqual(first.history.id, second.history.id)
        self.assertEqual(1, movie_count)
        self.assertEqual(1, history_count)
        self.assertEqual("1291992", movie["douban_subject_id"])
        self.assertEqual("末路狂花 Thelma & Louise", movie["display_title"])
        self.assertEqual("Thelma & Louise", movie["original_title"])
        self.assertEqual(["塞尔玛与路易丝"], movie["aka_titles"])
        self.assertEqual(9.0, float(movie["douban_rating"]))
        self.assertEqual(4.5, float(history["user_rating"]))
        self.assertEqual("updated", history["comment"])

    def test_reimport_same_source_row_with_changed_hash_updates_history(self) -> None:
        dsn = os.environ["MOVIES_POSTGRES_DSN"]
        with PostgresViewingHistoryRepository(dsn) as repository:
            _reset_schema(repository)
            repository.initialize_schema()

            first = repository.persist_confirmed_viewing_history(
                _confirmed(source_row_hash="hash-before", rating=4.0, comment="first"),
                _detail(rating=8.9),
            )
            second = repository.persist_confirmed_viewing_history(
                _confirmed(source_row_hash="hash-after", rating=4.5, comment="updated"),
                _detail(rating=9.0),
            )

            history_count = repository.connection.execute("SELECT COUNT(*) AS count FROM viewing_history").fetchone()[
                "count"
            ]
            history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(first.history.id, second.history.id)
        self.assertEqual(1, history_count)
        self.assertEqual("hash-after", history["source_row_hash"])
        self.assertEqual(4.5, float(history["user_rating"]))
        self.assertEqual("updated", history["comment"])

    def test_rejects_history_without_source_row_hash(self) -> None:
        dsn = os.environ["MOVIES_POSTGRES_DSN"]
        with PostgresViewingHistoryRepository(dsn) as repository:
            _reset_schema(repository)
            repository.initialize_schema()

            with self.assertRaisesRegex(ValueError, "source_row_hash"):
                repository.persist_confirmed_viewing_history(
                    _confirmed(source_row_hash=None),
                    _detail(),
                )


def _reset_schema(repository: PostgresViewingHistoryRepository) -> None:
    repository.connection.execute("DROP TABLE IF EXISTS feedback")
    repository.connection.execute("DROP TABLE IF EXISTS wishlist")
    repository.connection.execute("DROP TABLE IF EXISTS recommendation_items")
    repository.connection.execute("DROP TABLE IF EXISTS recommendation_sessions")
    repository.connection.execute("DROP TABLE IF EXISTS candidate_pool")
    repository.connection.execute("DROP TABLE IF EXISTS candidate_subject_queue")
    repository.connection.execute("DROP TABLE IF EXISTS viewing_history")
    repository.connection.execute("DROP TABLE IF EXISTS movies")


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
        title="Thelma & Louise",
        display_title="末路狂花 Thelma & Louise",
        original_title="Thelma & Louise",
        aka_titles=("塞尔玛与路易丝",),
        year=1991,
        directors=("Ridley Scott",),
        actors=("Geena Davis", "Susan Sarandon"),
        genres=("Drama", "Crime"),
        countries=("United States",),
        douban_rating=rating,
        douban_vote_count=353101,
        summary="A road movie.",
        poster_url="https://img.example/poster.webp",
        url=f"https://movie.douban.com/subject/{subject_id}/",
    )


if __name__ == "__main__":
    unittest.main()
