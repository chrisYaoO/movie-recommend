import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail


class SQLiteViewingHistoryRepositoryTest(unittest.TestCase):
    def test_schema_migration_preserves_existing_history_uuid(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            import sqlite3

            connection = sqlite3.connect(db_path)
            connection.execute(
                """CREATE TABLE viewing_history (
                       id TEXT PRIMARY KEY, movie_id TEXT, douban_subject_id TEXT NOT NULL,
                       watched_date TEXT, user_rating REAL NOT NULL, quality TEXT, comment TEXT,
                       source_row_checksum TEXT NOT NULL, source_sheet_name TEXT NOT NULL,
                       source_row_number INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """INSERT INTO viewing_history VALUES
                   ('existing-uuid', NULL, '1291992', '2026-01-01', 4.0, NULL, NULL,
                    'checksum', '2026', 2, 'now', 'now')"""
            )
            connection.commit()
            connection.close()

            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                row = repository.connection.execute(
                    "SELECT id, deleted_at FROM viewing_history"
                ).fetchone()
                outbox_exists = repository.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='sheet_sync_outbox'"
                ).fetchone()

        self.assertEqual("existing-uuid", row["id"])
        self.assertIsNone(row["deleted_at"])
        self.assertIsNotNone(outbox_exists)

    def test_persists_confirmed_viewing_history_without_raw_history_table(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                result = repository.persist_confirmed_viewing_history(
                    _confirmed(source_row_checksum="checksum-1"),
                    _detail(),
                )

                movie = repository.connection.execute("SELECT * FROM movies").fetchone()
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()
                raw_table = repository.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'viewing_history_raw'"
                ).fetchone()

        self.assertEqual("1291992", result.movie.douban_subject_id)
        self.assertEqual("1291992", movie["douban_subject_id"])
        self.assertEqual("Thelma and Louise", movie["title"])
        self.assertEqual(["Aka Title"], json.loads(movie["aka_titles_json"]))
        self.assertEqual(["Ridley Scott"], json.loads(movie["directors_json"]))
        self.assertEqual(["Drama", "Thriller", "Crime"], json.loads(movie["genres_json"]))
        self.assertEqual(9.0, movie["douban_rating"])
        self.assertEqual(353101, movie["douban_vote_count"])
        self.assertEqual(result.movie.id, history["movie_id"])
        self.assertEqual("1291992", history["douban_subject_id"])
        self.assertEqual("2026-03-19", history["watched_date"])
        self.assertEqual(4.2, history["user_rating"])
        self.assertEqual("1080p", history["quality"])
        self.assertEqual("test comment", history["comment"])
        self.assertEqual("checksum-1", history["source_row_checksum"])
        self.assertEqual("MOVIES.xlsx#2026", history["source_sheet_name"])
        self.assertIsNone(raw_table)
        self.assertNotIn("display_title", movie.keys())
        self.assertNotIn("original_title", movie.keys())

    def test_same_source_row_does_not_collapse_distinct_history_ids(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                first = repository.persist_confirmed_viewing_history(
                    _confirmed(source_row_checksum="checksum-1", rating=4.0, comment="first"),
                    _detail(rating=8.9),
                )
                second = repository.persist_confirmed_viewing_history(
                    _confirmed(source_row_checksum="checksum-1", rating=4.5, comment="updated"),
                    _detail(rating=9.0),
                )

                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]
                movie = repository.connection.execute("SELECT * FROM movies").fetchone()
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(first.movie.id, second.movie.id)
        self.assertNotEqual(first.history.id, second.history.id)
        self.assertEqual(1, movie_count)
        self.assertEqual(2, history_count)
        self.assertEqual(9.0, movie["douban_rating"])
        self.assertEqual(4.0, history["user_rating"])
        self.assertEqual("1291992", history["douban_subject_id"])
        self.assertEqual("first", history["comment"])

    def test_explicit_history_id_updates_without_changing_uuid(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                first = repository.persist_confirmed_viewing_history(
                    _confirmed(source_row_checksum="checksum-before", rating=4.0, comment="first"),
                    _detail(rating=8.9),
                )
                second = repository.persist_confirmed_viewing_history(
                    _confirmed(
                        source_row_checksum="checksum-after",
                        rating=4.5,
                        comment="updated",
                        history_id=first.history.id,
                    ),
                    _detail(rating=9.0),
                )

                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(first.history.id, second.history.id)
        self.assertEqual(1, history_count)
        self.assertEqual("checksum-after", history["source_row_checksum"])
        self.assertEqual("1291992", history["douban_subject_id"])
        self.assertEqual(4.5, history["user_rating"])
        self.assertEqual("updated", history["comment"])

    def test_rejects_history_without_source_row_checksum(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                with self.assertRaisesRegex(ValueError, "source_row_checksum"):
                    repository.persist_confirmed_viewing_history(
                        _confirmed(source_row_checksum=None),
                        _detail(),
                    )

    def test_history_mutations_replace_one_outbox_task_and_soft_delete(self) -> None:
        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                movie = repository.upsert_movie_detail(_detail())
                history = repository.save_viewing_history_and_enqueue(_confirmed(), movie.id)
                self.assertEqual("upsert", repository.find_pending_sheet_sync_tasks()[0].operation)

                updated = repository.update_viewing_history_and_enqueue(
                    history.id, date(2026, 4, 1), 4.5, "4K", "edited", "checksum-2"
                )
                deleted = repository.soft_delete_viewing_history_and_enqueue(history.id)
                tasks = repository.find_pending_sheet_sync_tasks()
                row = repository.connection.execute(
                    "SELECT deleted_at FROM viewing_history WHERE id = ?", (history.id,)
                ).fetchone()

        self.assertTrue(updated)
        self.assertTrue(deleted)
        self.assertEqual(1, len(tasks))
        self.assertEqual("delete", tasks[0].operation)
        self.assertIsNotNone(row["deleted_at"])

    def test_history_can_be_filtered_by_year_and_reversed(self) -> None:
        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                movie = repository.upsert_movie_detail(_detail())
                old = repository.upsert_viewing_history(_confirmed(watched_date=date(2025, 6, 1)), movie.id)
                newer = repository.upsert_viewing_history(_confirmed(watched_date=date(2026, 2, 1)), movie.id)
                newest = repository.upsert_viewing_history(_confirmed(watched_date=date(2026, 8, 1)), movie.id)

                descending = repository.find_active_viewing_history(year=2026)
                ascending = repository.find_active_viewing_history(year=2026, descending=False)

                self.assertEqual([newest.id, newer.id], [row.id for row in descending])
                self.assertEqual([newer.id, newest.id], [row.id for row in ascending])
                self.assertEqual(2, repository.count_active_viewing_history(2026))
                self.assertEqual([2026, 2025], repository.find_active_viewing_history_years())
                self.assertNotIn(old.id, [row.id for row in descending])

    def test_history_without_an_exact_date_uses_its_source_year(self) -> None:
        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                movie = repository.upsert_movie_detail(_detail())
                history = repository.upsert_viewing_history(
                    _confirmed(watched_date=None, source_sheet_name="2026"), movie.id
                )

                rows = repository.find_active_viewing_history(year=2026)

                self.assertEqual([history.id], [row.id for row in rows])
                self.assertEqual(1, repository.count_active_viewing_history(2026))
                self.assertEqual([2026], repository.find_active_viewing_history_years())

    def test_history_and_outbox_write_roll_back_together(self) -> None:
        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                movie = repository.upsert_movie_detail(_detail())
                original_enqueue = repository._enqueue_sheet_sync
                repository._enqueue_sheet_sync = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    repository.save_viewing_history_and_enqueue(_confirmed(), movie.id)
                repository._enqueue_sheet_sync = original_enqueue
                count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]

        self.assertEqual(0, count)

    def test_requires_matching_confirmed_subject_and_detail_subject(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                with self.assertRaisesRegex(ValueError, "does not match"):
                    repository.persist_confirmed_viewing_history(
                        _confirmed(subject_id="1291992", source_row_checksum="checksum-1"),
                        _detail(subject_id="wrong"),
                    )

    def test_backfills_candidate_source_labels_from_local_movie_titles(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "movies.db"
            with SQLiteViewingHistoryRepository(db_path) as repository:
                repository.initialize_schema()
                source_movie = repository.upsert_movie_detail(_detail(subject_id="source-1"))
                candidate_movie = repository.upsert_movie_detail(_detail(subject_id="candidate-1"))
                repository.upsert_candidate_subject(
                    "candidate-2",
                    "douban_recommendation",
                    "recommended_from:source-1",
                )
                repository.upsert_candidate_pool_entry(
                    candidate_movie.id,
                    "douban_recommendation",
                    "recommended_from:source-1",
                )
                repository.upsert_candidate_subject(
                    "candidate-3",
                    "douban_recommendation",
                    "recommended_from:missing-source",
                )

                updated_count = repository.backfill_candidate_source_labels_from_movies()
                queue_rows = repository.connection.execute(
                    """
                    SELECT douban_subject_id, source_label
                    FROM candidate_subject_queue
                    ORDER BY douban_subject_id
                    """
                ).fetchall()
                pool_row = repository.connection.execute(
                    "SELECT source_label FROM candidate_pool WHERE movie_id = ?",
                    (candidate_movie.id,),
                ).fetchone()

        self.assertEqual("source-1", source_movie.douban_subject_id)
        self.assertEqual(2, updated_count)
        self.assertEqual(
            [
                ("candidate-2", "recommended from Thelma and Louise"),
                ("candidate-3", None),
            ],
            [(row["douban_subject_id"], row["source_label"]) for row in queue_rows],
        )
        self.assertEqual("recommended from Thelma and Louise", pool_row["source_label"])


def _confirmed(
    subject_id: str = "1291992",
    source_row_checksum: str | None = "checksum-1",
    rating: float = 4.2,
    comment: str = "test comment",
    history_id: str | None = None,
    watched_date: date | None = date(2026, 3, 19),
    source_sheet_name: str = "MOVIES.xlsx#2026",
) -> ConfirmedViewingHistoryInput:
    return ConfirmedViewingHistoryInput(
        source_raw_id="raw-1",
        source_sheet_name=source_sheet_name,
        source_row_number=8,
        douban_subject_id=subject_id,
        watched_date=watched_date,
        user_rating=rating,
        source_row_checksum=source_row_checksum,
        quality="1080p",
        comment=comment,
        history_id=history_id,
    )


def _detail(subject_id: str = "1291992", rating: float = 9.0) -> DoubanMovieDetail:
    return DoubanMovieDetail(
        subject_id=subject_id,
        title="Thelma and Louise",
        display_title="Thelma and Louise",
        original_title="Thelma & Louise",
        aka_titles=("Aka Title",),
        year=1991,
        directors=("Ridley Scott",),
        actors=("Geena Davis", "Susan Sarandon"),
        genres=("Drama", "Thriller", "Crime"),
        countries=("United States",),
        douban_rating=rating,
        douban_vote_count=353101,
        summary="A road movie.",
        poster_url="https://img.example/poster.webp",
        url=f"https://movie.douban.com/subject/{subject_id}/",
    )


if __name__ == "__main__":
    unittest.main()



