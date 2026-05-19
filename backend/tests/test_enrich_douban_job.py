import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail
from backend.app.services.metadata_service import FakeDoubanDetailAdapter
from jobs.enrich_douban import enrich_douban_subjects


class EnrichDoubanJobTest(unittest.TestCase):
    def test_enrich_douban_subjects_uses_movie_table_and_continues_after_failure(self) -> None:
        existing = DoubanMovieDetail(subject_id="existing", title="Existing Movie")
        fresh = DoubanMovieDetail(subject_id="fresh", title="Fresh Movie")
        adapter = FakeDoubanDetailAdapter({"fresh": fresh})

        with TemporaryDirectory() as directory:
            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                with repository.connection:
                    repository.upsert_movie_detail(existing)

                result = enrich_douban_subjects(
                    ["existing", "fresh", "fresh", "missing"],
                    adapter,
                    repository,
                )

                fresh_row = repository.find_movie_by_subject_id("fresh")
                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]

        self.assertEqual(["fresh", "missing"], adapter.fetches)
        self.assertEqual("Fresh Movie", fresh_row.title)
        self.assertEqual(2, movie_count)
        self.assertEqual(1, result.fetched_count)
        self.assertEqual(2, result.existing_count)
        self.assertEqual(1, result.failed_count)
        self.assertEqual(
            ["existing", "fetched", "existing", "failed"],
            [item.status for item in result.items],
        )
        self.assertEqual("missing", result.items[-1].subject_id)
        self.assertIn("missing", result.items[-1].error or "")


if __name__ == "__main__":
    unittest.main()
