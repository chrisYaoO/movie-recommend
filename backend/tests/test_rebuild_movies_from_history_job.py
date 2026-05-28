import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail
from jobs.rebuild_movies_from_history import rebuild_movies_from_viewing_history


class RebuildMoviesFromHistoryJobTest(unittest.TestCase):
    def test_rebuilds_movies_backfills_history_and_enqueues_recommendations(self) -> None:
        with TemporaryDirectory() as directory:
            adapter = _FakePageDetailAdapter(
                {
                    "1291561": _detail("1291561", "Yi Yi"),
                },
                {
                    "1291561": """
                    <div id="recommendations">
                      <div class="recommendations-bd">
                        <a href="https://movie.douban.com/subject/2222996/">Still Walking</a>
                      </div>
                    </div>
                    喜欢这部电影的人也喜欢
                    """,
                },
            )
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                repository.upsert_viewing_history(_confirmed("1291561"), movie_id=None)

                summary = rebuild_movies_from_viewing_history(repository, adapter)

                movie = repository.connection.execute("SELECT * FROM movies").fetchone()
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()
                queued = repository.connection.execute(
                    "SELECT douban_subject_id, source_type, source_ref, source_subject_id FROM candidate_subject_queue"
                ).fetchone()

        self.assertEqual(1, summary.pending_subject_count)
        self.assertEqual(1, summary.attempted_count)
        self.assertEqual(1, summary.fetched_count)
        self.assertEqual(1, summary.backfilled_history_count)
        self.assertEqual(1, summary.recommendation_inserted_count)
        self.assertEqual("1291561", movie["douban_subject_id"])
        self.assertEqual(movie["id"], history["movie_id"])
        self.assertEqual(("2222996", "douban_recommendation", "recommended_from:1291561", "1291561"), tuple(queued))

    def test_dry_run_does_not_fetch_or_write(self) -> None:
        with TemporaryDirectory() as directory:
            adapter = _FakePageDetailAdapter({}, {})
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                repository.upsert_viewing_history(_confirmed("1291561"), movie_id=None)

                summary = rebuild_movies_from_viewing_history(repository, adapter, dry_run=True)

                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                history = repository.connection.execute("SELECT * FROM viewing_history").fetchone()

        self.assertEqual(1, summary.pending_subject_count)
        self.assertEqual(0, summary.attempted_count)
        self.assertEqual(0, movie_count)
        self.assertIsNone(history["movie_id"])
        self.assertEqual([], adapter.fetches)


class _FakePageDetailAdapter:
    def __init__(self, details_by_subject_id, pages_by_subject_id) -> None:
        self.details_by_subject_id = details_by_subject_id
        self.pages_by_subject_id = pages_by_subject_id
        self.last_page_source = None
        self.fetches = []

    def fetch(self, subject_id):
        self.fetches.append(subject_id)
        self.last_page_source = self.pages_by_subject_id.get(subject_id)
        return self.details_by_subject_id[subject_id]


def _confirmed(subject_id: str) -> ConfirmedViewingHistoryInput:
    return ConfirmedViewingHistoryInput(
        source_raw_id=f"raw-{subject_id}",
        source_sheet_name="2026",
        source_row_number=2,
        douban_subject_id=subject_id,
        watched_date=date(2026, 5, 27),
        user_rating=5.0,
        source_row_checksum=f"checksum-{subject_id}",
    )


def _detail(subject_id: str, title: str) -> DoubanMovieDetail:
    return DoubanMovieDetail(
        subject_id=subject_id,
        title=title,
        year=2000,
        directors=("Director",),
        actors=(),
        genres=("Drama",),
        countries=("Country",),
        douban_rating=8.8,
        douban_vote_count=1000,
        url=f"https://movie.douban.com/subject/{subject_id}/",
    )


if __name__ == "__main__":
    unittest.main()
