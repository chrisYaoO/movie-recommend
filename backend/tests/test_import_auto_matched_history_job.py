import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import Workbook

from backend.app.config import resolve_postgres_dsn
from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail, DoubanSearchResult
from backend.app.services.import_service import InMemoryViewingHistoryRawRepository, ViewingHistoryImportService
from backend.app.services.matching_service import CachedDoubanSearchAdapter, FakeDoubanSearchAdapter, InMemoryDoubanSearchCache
from backend.app.services.metadata_service import FakeDoubanDetailAdapter
from jobs.import_auto_matched_history import (
    _completed_hashes,
    import_auto_matched_history,
    import_metadata_auto_matches_resumable,
    _replace_with_retries,
    retry_no_year_match_no_matches,
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

    def test_replace_with_retries_tolerates_temporary_permission_error(self) -> None:
        with TemporaryDirectory() as directory:
            temp_path = Path(directory) / "progress.json.tmp"
            target_path = Path(directory) / "progress.json"
            temp_path.write_text('{"items": []}', encoding="utf-8")
            original_replace = Path.replace
            calls = 0

            def flaky_replace(self, target):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError("locked")
                return original_replace(self, target)

            with patch.object(Path, "replace", flaky_replace):
                _replace_with_retries(temp_path, target_path, attempts=2, delay_seconds=0)

            self.assertEqual(2, calls)
            self.assertEqual('{"items": []}', target_path.read_text(encoding="utf-8"))

    def test_completed_hashes_include_manual_review_terminal_statuses(self) -> None:
        state = {
            "items": [
                {"source_row_checksum": "auto", "status": "auto_matched_persisted"},
                {"source_row_checksum": "needs-review", "status": "needs_review"},
                {"source_row_checksum": "no-match", "status": "no_match"},
                {"source_row_checksum": "review-confirmed", "status": "review_confirmed_persisted"},
                {"source_row_checksum": "review-rejected", "status": "review_rejected"},
                {"source_row_checksum": "manual-persisted", "status": "manual_id_persisted"},
                {"source_row_checksum": "manual-rejected", "status": "manual_id_rejected"},
                {"source_row_checksum": "pending", "status": "pending"},
            ]
        }

        self.assertEqual(
            {
                "auto",
                "needs-review",
                "no-match",
                "review-confirmed",
                "review-rejected",
                "manual-persisted",
                "manual-rejected",
            },
            _completed_hashes(state),
        )

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
                    state_path=Path(directory) / "progress.json",
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

    def test_import_auto_detects_subject_id_rows_without_searching_them(self) -> None:
        with TemporaryDirectory() as directory:
            excel_path = Path(directory) / "MOVIES.xlsx"
            _write_workbook(excel_path)
            search_adapter = FakeDoubanSearchAdapter(
                {"Still Walking": [DoubanSearchResult(subject_id="2222996", title="Still Walking", year=2008)]}
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
                    state_path=Path(directory) / "progress.json",
                )
                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]

        self.assertEqual(4, result.summary.mapped_candidate_count)
        self.assertEqual(2, result.summary.auto_matched_count)
        self.assertEqual(2, result.summary.persisted_count)
        self.assertEqual(["Still Walking", "Bitter Sweet Life", "Unknown Movie"], [item.title for item in search_adapter.searches])
        self.assertEqual(["1291561", "2222996"], detail_adapter.fetches)
        self.assertEqual(2, movie_count)

    def test_import_persists_subject_id_rows_before_metadata_search_failure(self) -> None:
        with TemporaryDirectory() as directory:
            excel_path = Path(directory) / "MOVIES.xlsx"
            _write_workbook(excel_path)
            search_adapter = FailingDoubanSearchAdapter()
            detail_adapter = FakeDoubanDetailAdapter({"1291561": _detail("1291561", "Yi Yi")})

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                with self.assertRaisesRegex(RuntimeError, "search blocked"):
                    import_auto_matched_history(
                        excel_path,
                        search_adapter,
                        detail_adapter,
                        repository,
                        state_path=Path(directory) / "progress.json",
                    )
                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]

        self.assertEqual(["Still Walking"], [item.title for item in search_adapter.searches])
        self.assertEqual(["1291561"], detail_adapter.fetches)
        self.assertEqual(1, movie_count)
        self.assertEqual(1, history_count)

    def test_import_status_reports_metadata_cache_and_failed_row(self) -> None:
        with TemporaryDirectory() as directory:
            excel_path = Path(directory) / "MOVIES.xlsx"
            _write_workbook(excel_path)
            search_adapter = CachedDoubanSearchAdapter(FailingDoubanSearchAdapter(), InMemoryDoubanSearchCache())
            detail_adapter = FakeDoubanDetailAdapter({"1291561": _detail("1291561", "Yi Yi")})
            status_messages: list[str] = []

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                with self.assertRaisesRegex(RuntimeError, "search blocked"):
                    import_auto_matched_history(
                        excel_path,
                        search_adapter,
                        detail_adapter,
                        repository,
                        state_path=Path(directory) / "progress.json",
                        status_writer=status_messages.append,
                    )

        output = "\n".join(status_messages)
        self.assertIn("[metadata] rows without id: 3", output)
        self.assertIn("[metadata] cache hits: 0", output)
        self.assertIn("[metadata] cache misses: 3", output)
        self.assertNotIn("[metadata] first miss:", output)
        self.assertIn("[match] metadata 1/3: row 3, title = Still Walking, year = 2008, cache = miss", output)
        self.assertIn("[error] metadata row failed: row 3, title = Still Walking, year = 2008", output)
        self.assertIn("[resume] attempted this run: 1, auto_matched=0, needs_review=0, no_match=0, persisted=0, failed=1", output)

    def test_resumable_metadata_search_persists_each_auto_match_and_resumes(self) -> None:
        with TemporaryDirectory() as directory:
            excel_path = Path(directory) / "MOVIES.xlsx"
            state_path = Path(directory) / "progress.json"
            _write_workbook(excel_path)
            status_messages: list[str] = []
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
                    status_writer=status_messages.append,
                )
                first_movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                second = import_metadata_auto_matches_resumable(
                    excel_path,
                    search_adapter,
                    detail_adapter,
                    repository,
                    state_path,
                    status_writer=status_messages.append,
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
        output = "\n".join(status_messages)
        self.assertIn("[match] status = auto_matched, score = 0.95", output)
        self.assertNotIn("[match] row 3 status", output)

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

    def test_resumable_metadata_search_skips_completed_source_row_when_hash_changes(self) -> None:
        with TemporaryDirectory() as directory:
            excel_path = Path(directory) / "MOVIES.xlsx"
            state_path = Path(directory) / "progress.json"
            _write_workbook(excel_path)
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "source_row_checksum": "old-hash",
                                "source_sheet_name": "2026",
                                "source_row_number": 3,
                                "status": "auto_matched_persisted",
                            },
                            {
                                "source_row_checksum": "old-checksum-2",
                                "source_sheet_name": "2026",
                                "source_row_number": 4,
                                "status": "needs_review",
                            },
                            {
                                "source_row_checksum": "old-checksum-3",
                                "source_sheet_name": "2026",
                                "source_row_number": 5,
                                "status": "no_match",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            search_adapter = FakeDoubanSearchAdapter()
            detail_adapter = FakeDoubanDetailAdapter()

            with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
                repository.initialize_schema()
                result = import_metadata_auto_matches_resumable(
                    excel_path,
                    search_adapter,
                    detail_adapter,
                    repository,
                    state_path,
                    limit=1,
                )

        self.assertEqual(3, result.summary.already_completed_count)
        self.assertIsNone(result.summary.next_index)
        self.assertEqual([], search_adapter.searches)

    def test_retry_no_year_match_no_matches_moves_search_hits_back_to_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            excel_path = root / "MOVIES.xlsx"
            state_path = root / "progress.json"
            _write_workbook(excel_path)
            import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
            import_service.import_excel(excel_path)
            candidates = import_service.to_viewing_history_candidates().candidates
            still_walking = next(candidate for candidate in candidates if candidate.title == "Still Walking")
            unknown = next(candidate for candidate in candidates if candidate.title == "Unknown Movie")
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            _no_year_match_item(still_walking),
                            _no_year_match_item(unknown),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            search_adapter = FakeDoubanSearchAdapter(
                {
                    "Still Walking": [
                        DoubanSearchResult(subject_id="wrong-year", title="Still Walking", year=2019),
                    ],
                }
            )

            result = retry_no_year_match_no_matches(excel_path, search_adapter, state_path)

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(2, result.candidate_count)
        self.assertEqual(2, result.attempted_count)
        self.assertEqual(1, result.updated_to_needs_review_count)
        self.assertEqual(1, result.kept_no_match_count)
        self.assertEqual("needs_review", progress["items"][0]["status"])
        self.assertEqual("auto_matched", progress["items"][0]["match_status"])
        self.assertEqual(["title_exact_year_differs"], progress["items"][0]["match_reasons"])
        self.assertEqual("wrong-year", progress["items"][0]["candidate_subject_id"])
        self.assertEqual("no_match", progress["items"][1]["status"])
        self.assertEqual(["douban_search_no_results"], progress["items"][1]["match_reasons"])


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


def _no_year_match_item(candidate) -> dict:
    return {
        "source_row_checksum": candidate.source_row_checksum,
        "source_raw_id": candidate.source_raw_id,
        "source_sheet_name": candidate.source_sheet_name,
        "source_row_number": candidate.source_row_number,
        "title": candidate.title,
        "release_year": candidate.release_year,
        "match_status": "no_match",
        "match_score": 0.0,
        "match_reasons": ["douban_search_no_year_match"],
        "candidate_subject_id": None,
        "candidate_title": candidate.title,
        "candidate_year": candidate.release_year,
        "candidate_director": candidate.director,
        "status": "no_match",
    }


class FailingDoubanSearchAdapter:
    def __init__(self) -> None:
        self.searches = []

    def search(self, match_input):
        self.searches.append(match_input)
        raise RuntimeError("search blocked")


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


