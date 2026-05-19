import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import Workbook

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail, DoubanSearchResult
from backend.app.services.matching_service import FakeDoubanSearchAdapter
from backend.app.services.metadata_service import FakeDoubanDetailAdapter
from jobs.import_auto_matched_history import (
    import_auto_matched_history,
    import_metadata_auto_matches_resumable,
    resolve_postgres_dsn,
)


class ImportAutoMatchedHistoryJobTest(unittest.TestCase):
    def test_resolve_postgres_dsn_uses_env_when_cli_receives_powershell_literal(self) -> None:
        with patch.dict("os.environ", {"MOVIES_POSTGRES_DSN": "postgresql://postgres:secret@localhost:5432/movies"}):
            self.assertEqual(
                "postgresql://postgres:secret@localhost:5432/movies",
                resolve_postgres_dsn("$env:MOVIES_POSTGRES_DSN"),
            )

    def test_resolve_postgres_dsn_uses_local_config_when_env_is_missing(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / ".env"
            config_path.write_text(
                "MOVIES_POSTGRES_DSN=postgresql://postgres:secret@localhost:5432/movies\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(
                    "postgresql://postgres:secret@localhost:5432/movies",
                    resolve_postgres_dsn(None, config_path),
                )

    def test_resolve_postgres_dsn_requires_value_when_env_is_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "PostgreSQL DSN is required"):
                resolve_postgres_dsn(None, Path("missing.env"))

    def test_resolve_postgres_dsn_reports_literal_env_reference_without_env(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "passed literally"):
                resolve_postgres_dsn("$env:MOVIES_POSTGRES_DSN", Path("missing.env"))

    def test_imports_only_auto_matched_history_and_skips_review_or_no_match(self) -> None:
        with TemporaryDirectory() as directory:
            excel_path = Path(directory) / "MOVIES.xlsx"
            _write_workbook(excel_path)
            search_adapter = FakeDoubanSearchAdapter(
                {
                    "Still Walking": [DoubanSearchResult(subject_id="2222996", title="Still Walking", year=2008)],
                    "Bitter Sweet Life": [
                        DoubanSearchResult(subject_id="review-id", title="Bittersweet Life", year=2025)
                    ],
                }
            )
            detail_adapter = FakeDoubanDetailAdapter(
                {
                    "1291561": _detail("1291561", "Yi Yi"),
                    "2222996": _detail("2222996", "Still Walking"),
                }
            )

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                result = import_auto_matched_history(
                    excel_path,
                    search_adapter,
                    detail_adapter,
                    repository,
                )
                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]

        self.assertEqual(4, result.summary.imported_count)
        self.assertEqual(4, result.summary.mapped_candidate_count)
        self.assertEqual(2, result.summary.auto_matched_count)
        self.assertEqual(1, result.summary.needs_review_skipped_count)
        self.assertEqual(1, result.summary.no_match_skipped_count)
        self.assertEqual(2, result.summary.persisted_count)
        self.assertEqual(2, result.summary.fetched_count)
        self.assertEqual(0, result.summary.failed_count)
        self.assertEqual(["Still Walking", "Bitter Sweet Life", "Unknown Movie"], [item.title for item in search_adapter.searches])
        self.assertEqual(["1291561", "2222996"], detail_adapter.fetches)
        self.assertEqual(2, movie_count)
        self.assertEqual(2, history_count)

    def test_subject_id_only_mode_skips_metadata_search_rows(self) -> None:
        with TemporaryDirectory() as directory:
            excel_path = Path(directory) / "MOVIES.xlsx"
            _write_workbook(excel_path)
            search_adapter = FakeDoubanSearchAdapter()
            detail_adapter = FakeDoubanDetailAdapter({"1291561": _detail("1291561", "Yi Yi")})

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                result = import_auto_matched_history(
                    excel_path,
                    search_adapter,
                    detail_adapter,
                    repository,
                    subject_id_only=True,
                )
                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]

        self.assertEqual(1, result.summary.mapped_candidate_count)
        self.assertEqual(1, result.summary.auto_matched_count)
        self.assertEqual(1, result.summary.persisted_count)
        self.assertEqual([], search_adapter.searches)
        self.assertEqual(["1291561"], detail_adapter.fetches)
        self.assertEqual(1, movie_count)

    def test_resumable_metadata_search_persists_each_auto_match_and_resumes(self) -> None:
        with TemporaryDirectory() as directory:
            excel_path = Path(directory) / "MOVIES.xlsx"
            state_path = Path(directory) / "progress.json"
            _write_workbook(excel_path)
            search_adapter = FakeDoubanSearchAdapter(
                {
                    "Still Walking": [DoubanSearchResult(subject_id="2222996", title="Still Walking", year=2008)],
                    "Bitter Sweet Life": [
                        DoubanSearchResult(subject_id="review-id", title="Bittersweet Life", year=2025)
                    ],
                }
            )
            detail_adapter = FakeDoubanDetailAdapter({"2222996": _detail("2222996", "Still Walking")})

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                first = import_metadata_auto_matches_resumable(
                    excel_path,
                    search_adapter,
                    detail_adapter,
                    repository,
                    state_path,
                    limit=1,
                )
                first_movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                second = import_metadata_auto_matches_resumable(
                    excel_path,
                    search_adapter,
                    detail_adapter,
                    repository,
                    state_path,
                )
                final_movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                progress = state_path.read_text(encoding="utf-8")

        self.assertEqual(3, first.summary.metadata_candidate_count)
        self.assertEqual(1, first.summary.attempted_count)
        self.assertEqual(1, first.summary.persisted_count)
        self.assertEqual(1, first_movie_count)
        self.assertEqual(2, second.summary.attempted_count)
        self.assertEqual(1, second.summary.needs_review_count)
        self.assertEqual(1, second.summary.no_match_count)
        self.assertEqual(1, final_movie_count)
        self.assertIn("auto_matched_persisted", progress)
        self.assertIn("needs_review", progress)
        self.assertIn("no_match", progress)

    def test_resumable_metadata_search_records_persistence_failure_once(self) -> None:
        with TemporaryDirectory() as directory:
            excel_path = Path(directory) / "MOVIES.xlsx"
            state_path = Path(directory) / "progress.json"
            _write_workbook(excel_path)
            search_adapter = FakeDoubanSearchAdapter(
                {"Still Walking": [DoubanSearchResult(subject_id="2222996", title="Still Walking", year=2008)]}
            )
            detail_adapter = FakeDoubanDetailAdapter()

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                with self.assertRaises(RuntimeError):
                    import_metadata_auto_matches_resumable(
                        excel_path,
                        search_adapter,
                        detail_adapter,
                        repository,
                        state_path,
                        limit=1,
                    )
                progress = state_path.read_text(encoding="utf-8")

        self.assertEqual(1, progress.count('"status": "failed"'))


def _write_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "2026"
    sheet.append(["Date", "Name", "Director", "Year", "Rating", "Quality", "Comment", "movie_id"])
    sheet.append(["2026-01-01", "Yi Yi", "Edward Yang", 2000, 5.0, "1080p", "favorite", 1291561])
    sheet.append(["2026-01-02", "Still Walking", "Hirokazu Kore-eda", 2008, 4.5, "1080p", "good", None])
    sheet.append(["2026-01-03", "Bitter Sweet Life", "Director", 2025, 4.0, "1080p", "review", None])
    sheet.append(["2026-01-04", "Unknown Movie", "Director", 1990, 4.0, "1080p", "missing", None])
    workbook.save(path)


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
