import json
from contextlib import redirect_stdout
from io import StringIO
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import Workbook

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail, DoubanSearchResult
from backend.app.services.import_service import InMemoryViewingHistoryRawRepository, ViewingHistoryImportService
from backend.app.services.matching_service import FakeDoubanSearchAdapter
from backend.app.services.metadata_service import FakeDoubanDetailAdapter
from jobs.review_matched_history import (
    MANUAL_ID_PERSISTED_STATUS,
    MANUAL_ID_REJECTED_STATUS,
    REVIEW_CONFIRMED_STATUS,
    REVIEW_REJECTED_STATUS,
    _write_state,
    batch_search_rejected_or_no_match_history,
    resolve_rejected_or_no_match_history,
    review_matched_history,
)


class ReviewMatchedHistoryJobTest(unittest.TestCase):
    def test_confirms_and_rejects_needs_review_rows(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            excel_path = root / "MOVIES.xlsx"
            state_path = root / "progress.json"
            _write_workbook(excel_path)
            import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
            import_service.import_excel(excel_path)
            candidates = import_service.to_viewing_history_candidates().candidates
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            _review_item(
                                source_row_hash=candidates[0].source_row_hash,
                                source_row_number=2,
                                candidate_subject_id="subject-confirm",
                            ),
                            _review_item(
                                source_row_hash=candidates[1].source_row_hash,
                                source_row_number=3,
                                candidate_subject_id="subject-reject",
                            ),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            detail_adapter = FakeDoubanDetailAdapter(
                {
                    "subject-confirm": _detail("subject-confirm", "Confirmed Title"),
                    "subject-reject": _detail("subject-reject", "Rejected Title"),
                }
            )

            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                answers = iter(["", "1"])
                stdout = StringIO()
                with redirect_stdout(stdout):
                    result = review_matched_history(
                        excel_path,
                        state_path,
                        detail_adapter,
                        repository,
                        input_func=lambda _: next(answers),
                    )
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]
                output = stdout.getvalue()

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(2, result.review_candidate_count)
        self.assertEqual(1, result.confirmed_count)
        self.assertEqual(1, result.rejected_count)
        self.assertEqual(0, result.remaining_count)
        self.assertEqual(1, history_count)
        self.assertEqual(REVIEW_CONFIRMED_STATUS, progress["items"][0]["status"])
        self.assertEqual(REVIEW_REJECTED_STATUS, progress["items"][1]["status"])
        self.assertEqual("Candidate 2", progress["items"][0]["candidate_title"])
        self.assertEqual("Confirmed Title", progress["items"][0]["persisted_title"])
        self.assertEqual("Candidate 3", progress["items"][1]["candidate_title"])
        self.assertEqual(["subject-confirm"], detail_adapter.fetches)
        self.assertIn("[review] pending=2, this_run=2", output)
        self.assertIn("[review] 1/2", output)
        self.assertIn("[review] 2/2", output)

    def test_quit_persists_already_queued_confirmations(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            excel_path = root / "MOVIES.xlsx"
            state_path = root / "progress.json"
            _write_workbook(excel_path)
            import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
            import_service.import_excel(excel_path)
            candidates = import_service.to_viewing_history_candidates().candidates
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            _review_item(
                                source_row_hash=candidates[0].source_row_hash,
                                source_row_number=2,
                                candidate_subject_id="subject-confirm",
                            ),
                            _review_item(
                                source_row_hash=candidates[1].source_row_hash,
                                source_row_number=3,
                                candidate_subject_id="subject-reject",
                            ),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            detail_adapter = FakeDoubanDetailAdapter(
                {"subject-confirm": _detail("subject-confirm", "Confirmed Title")}
            )

            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                answers = iter(["", "q"])
                with redirect_stdout(StringIO()):
                    result = review_matched_history(
                        excel_path,
                        state_path,
                        detail_adapter,
                        repository,
                        input_func=lambda _: next(answers),
                    )
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(1, result.confirmed_count)
        self.assertEqual(1, result.remaining_count)
        self.assertEqual(1, history_count)
        self.assertEqual(REVIEW_CONFIRMED_STATUS, progress["items"][0]["status"])
        self.assertEqual("needs_review", progress["items"][1]["status"])
        self.assertEqual(["subject-confirm"], detail_adapter.fetches)

    def test_recovers_review_item_when_saved_hash_is_stale(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            excel_path = root / "MOVIES.xlsx"
            state_path = root / "progress.json"
            _write_workbook(excel_path)
            import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
            import_service.import_excel(excel_path)
            candidates = import_service.to_viewing_history_candidates().candidates
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                **_review_item(
                                    source_row_hash="stale-hash",
                                    source_row_number=2,
                                    candidate_subject_id="subject-confirm",
                                ),
                                "review_status": "failed",
                                "review_error": "source row hash not found in Excel import",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            detail_adapter = FakeDoubanDetailAdapter({"subject-confirm": _detail("subject-confirm", "Confirmed Title")})

            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                answers = iter([""])
                with redirect_stdout(StringIO()):
                    result = review_matched_history(
                        excel_path,
                        state_path,
                        detail_adapter,
                        repository,
                        input_func=lambda _: next(answers),
                    )

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(1, result.confirmed_count)
        self.assertEqual(REVIEW_CONFIRMED_STATUS, progress["items"][0]["status"])
        self.assertEqual(candidates[0].source_row_hash, progress["items"][0]["source_row_hash"])
        self.assertNotIn("review_status", progress["items"][0])
        self.assertNotIn("review_error", progress["items"][0])

    def test_recovers_review_item_when_saved_sheet_name_is_stale(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            excel_path = root / "MOVIES.xlsx"
            state_path = root / "progress.json"
            _write_workbook(excel_path)
            import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
            import_service.import_excel(excel_path)
            candidates = import_service.to_viewing_history_candidates().candidates
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                **_review_item(
                                    source_row_hash="stale-hash",
                                    source_row_number=99,
                                    candidate_subject_id="subject-confirm",
                                ),
                                "source_file": "MOVIES.xlsx#OldSheet",
                                "title": "Excel Confirm",
                                "release_year": 2026,
                                "review_status": "failed",
                                "review_error": "source row hash not found in Excel import",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            detail_adapter = FakeDoubanDetailAdapter({"subject-confirm": _detail("subject-confirm", "Confirmed Title")})

            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                answers = iter([""])
                with redirect_stdout(StringIO()):
                    result = review_matched_history(
                        excel_path,
                        state_path,
                        detail_adapter,
                        repository,
                        input_func=lambda _: next(answers),
                    )

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(1, result.confirmed_count)
        self.assertEqual(candidates[0].source_row_hash, progress["items"][0]["source_row_hash"])
        self.assertEqual("MOVIES.xlsx#2026", progress["items"][0]["source_file"])
        self.assertEqual(2, progress["items"][0]["source_row_number"])
        self.assertEqual(REVIEW_CONFIRMED_STATUS, progress["items"][0]["status"])

    def test_skips_duplicate_pending_row_when_hash_is_already_persisted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            excel_path = root / "MOVIES.xlsx"
            state_path = root / "progress.json"
            _write_workbook(excel_path)
            import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
            import_service.import_excel(excel_path)
            candidates = import_service.to_viewing_history_candidates().candidates
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                **_review_item(
                                    source_row_hash=candidates[0].source_row_hash,
                                    source_row_number=2,
                                    candidate_subject_id="subject-confirm",
                                ),
                                "status": REVIEW_CONFIRMED_STATUS,
                                "movie_id": "movie-1",
                                "viewing_history_id": "history-1",
                                "persisted_title": "Confirmed Title",
                            },
                            _review_item(
                                source_row_hash=candidates[0].source_row_hash,
                                source_row_number=2,
                                candidate_subject_id="subject-confirm",
                            ),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            detail_adapter = FakeDoubanDetailAdapter({"subject-confirm": _detail("subject-confirm", "Confirmed Title")})

            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                with redirect_stdout(StringIO()):
                    result = review_matched_history(
                        excel_path,
                        state_path,
                        detail_adapter,
                        repository,
                        input_func=lambda _: self.fail("duplicate pending row should not prompt"),
                    )
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(0, result.confirmed_count)
        self.assertEqual(0, result.remaining_count)
        self.assertEqual(0, history_count)
        self.assertEqual(REVIEW_CONFIRMED_STATUS, progress["items"][1]["status"])
        self.assertEqual("history-1", progress["items"][1]["viewing_history_id"])
        self.assertEqual(candidates[0].source_row_hash, progress["items"][1]["duplicate_of_source_row_hash"])
        self.assertEqual([], detail_adapter.fetches)

    def test_skips_duplicate_pending_row_in_same_persistence_batch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            excel_path = root / "MOVIES.xlsx"
            state_path = root / "progress.json"
            _write_workbook(excel_path)
            import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
            import_service.import_excel(excel_path)
            candidates = import_service.to_viewing_history_candidates().candidates
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            _review_item(
                                source_row_hash=candidates[0].source_row_hash,
                                source_row_number=2,
                                candidate_subject_id="subject-confirm",
                            ),
                            _review_item(
                                source_row_hash=candidates[0].source_row_hash,
                                source_row_number=2,
                                candidate_subject_id="subject-confirm",
                            ),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            detail_adapter = FakeDoubanDetailAdapter({"subject-confirm": _detail("subject-confirm", "Confirmed Title")})

            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                answers = iter(["", ""])
                with redirect_stdout(StringIO()):
                    result = review_matched_history(
                        excel_path,
                        state_path,
                        detail_adapter,
                        repository,
                        input_func=lambda _: next(answers),
                    )
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(1, result.confirmed_count)
        self.assertEqual(1, history_count)
        self.assertEqual(["subject-confirm"], detail_adapter.fetches)
        self.assertEqual(REVIEW_CONFIRMED_STATUS, progress["items"][0]["status"])
        self.assertEqual(REVIEW_CONFIRMED_STATUS, progress["items"][1]["status"])
        self.assertEqual(progress["items"][0]["viewing_history_id"], progress["items"][1]["viewing_history_id"])
        self.assertEqual(candidates[0].source_row_hash, progress["items"][1]["duplicate_of_source_row_hash"])

    def test_resolves_rejected_and_no_match_rows_by_manual_subject_id(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            excel_path = root / "MOVIES.xlsx"
            state_path = root / "progress.json"
            _write_workbook(excel_path)
            import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
            import_service.import_excel(excel_path)
            candidates = import_service.to_viewing_history_candidates().candidates
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                **_review_item(
                                    source_row_hash=candidates[0].source_row_hash,
                                    source_row_number=2,
                                    candidate_subject_id="",
                                ),
                                "status": "no_match",
                                "candidate_subject_id": None,
                                "candidate_title": None,
                            },
                            {
                                **_review_item(
                                    source_row_hash=candidates[1].source_row_hash,
                                    source_row_number=3,
                                    candidate_subject_id="subject-rejected",
                                ),
                                "status": REVIEW_REJECTED_STATUS,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            detail_adapter = FakeDoubanDetailAdapter({"1291556": _detail("1291556", "Manual Title")})

            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                answers = iter(["1291556", "", "x"])
                stdout = StringIO()
                with redirect_stdout(stdout):
                    result = resolve_rejected_or_no_match_history(
                        excel_path,
                        state_path,
                        detail_adapter,
                        repository,
                        input_func=lambda _: next(answers),
                    )
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]
                output = stdout.getvalue()

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(2, result.review_candidate_count)
        self.assertEqual(1, result.confirmed_count)
        self.assertEqual(1, result.rejected_count)
        self.assertEqual(0, result.remaining_count)
        self.assertEqual(1, history_count)
        self.assertEqual(MANUAL_ID_PERSISTED_STATUS, progress["items"][0]["status"])
        self.assertEqual("1291556", progress["items"][0]["manual_id_subject_id"])
        self.assertEqual("Manual Title", progress["items"][0]["persisted_title"])
        self.assertEqual(MANUAL_ID_REJECTED_STATUS, progress["items"][1]["status"])
        self.assertEqual("discarded_without_subject_id", progress["items"][1]["manual_id_decision"])
        self.assertEqual(["1291556"], detail_adapter.fetches)
        self.assertIn("[manual-id] pending=2, this_run=2", output)
        self.assertIn("[manual-id] 1/2", output)
        self.assertIn("[manual-id] 2/2", output)

    def test_resolve_flow_can_search_no_match_again_and_return_to_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            excel_path = root / "MOVIES.xlsx"
            state_path = root / "progress.json"
            _write_workbook(excel_path)
            import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
            import_service.import_excel(excel_path)
            candidates = import_service.to_viewing_history_candidates().candidates
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                **_review_item(
                                    source_row_hash=candidates[0].source_row_hash,
                                    source_row_number=2,
                                    candidate_subject_id="",
                                ),
                                "status": "no_match",
                                "candidate_subject_id": None,
                                "candidate_title": None,
                                "match_reasons": ["douban_search_no_results"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            search_adapter = FakeDoubanSearchAdapter(
                {"Excel Confirm": [DoubanSearchResult(subject_id="retry-id", title="Retry Candidate", year=2025)]}
            )

            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                answers = iter(["a"])
                stdout = StringIO()
                with redirect_stdout(stdout):
                    result = resolve_rejected_or_no_match_history(
                        excel_path,
                        state_path,
                        FakeDoubanDetailAdapter(),
                        repository,
                        search_adapter=search_adapter,
                        input_func=lambda _: next(answers),
                    )
                output = stdout.getvalue()

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(0, result.confirmed_count)
        self.assertEqual("needs_review", progress["items"][0]["status"])
        self.assertEqual("retry-id", progress["items"][0]["candidate_subject_id"])
        self.assertEqual(["douban_search_no_year_match"], progress["items"][0]["match_reasons"])
        self.assertIn("Fresh Douban search status: needs_review, progress status: needs_review", output)
        self.assertIn("candidate_subject_id: retry-id", output)
        self.assertIn("candidate_title: Retry Candidate", output)
        self.assertIn("candidate_year: 2025", output)

    def test_batch_search_rejected_and_no_match_updates_rows_without_prompts(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            excel_path = root / "MOVIES.xlsx"
            state_path = root / "progress.json"
            _write_workbook(excel_path)
            import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
            import_service.import_excel(excel_path)
            candidates = import_service.to_viewing_history_candidates().candidates
            state_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                **_review_item(
                                    source_row_hash=candidates[0].source_row_hash,
                                    source_row_number=2,
                                    candidate_subject_id="",
                                ),
                                "status": "no_match",
                                "candidate_subject_id": None,
                                "candidate_title": None,
                                "match_reasons": ["douban_search_no_results"],
                            },
                            {
                                **_review_item(
                                    source_row_hash=candidates[1].source_row_hash,
                                    source_row_number=3,
                                    candidate_subject_id="subject-rejected",
                                ),
                                "status": REVIEW_REJECTED_STATUS,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            search_adapter = FakeDoubanSearchAdapter(
                {
                    "Excel Confirm": [
                        DoubanSearchResult(subject_id="retry-id", title="Retry Candidate", year=2025)
                    ],
                    "Excel Reject": [
                        DoubanSearchResult(subject_id="subject-rejected", title="Excel Reject", year=2026)
                    ],
                }
            )
            detail_adapter = FakeDoubanDetailAdapter({"subject-rejected": _detail("subject-rejected", "Excel Reject")})

            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                stdout = StringIO()
                with redirect_stdout(stdout):
                    result = batch_search_rejected_or_no_match_history(
                        excel_path,
                        state_path,
                        detail_adapter,
                        repository,
                        search_adapter=search_adapter,
                    )
                history_count = repository.connection.execute("SELECT COUNT(*) FROM viewing_history").fetchone()[0]
                output = stdout.getvalue()

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(2, result.review_candidate_count)
        self.assertEqual(1, result.confirmed_count)
        self.assertEqual(0, result.remaining_count)
        self.assertEqual(1, history_count)
        self.assertEqual("needs_review", progress["items"][0]["status"])
        self.assertEqual("retry-id", progress["items"][0]["candidate_subject_id"])
        self.assertEqual("auto_matched_persisted", progress["items"][1]["status"])
        self.assertEqual(["subject-rejected"], detail_adapter.fetches)
        self.assertIn("[batch-search-again] 1/2", output)
        self.assertIn("candidate_subject_id: retry-id", output)
        self.assertIn("Fresh Douban search status: auto_matched", output)

    def test_write_state_tolerates_temporary_permission_error(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "progress.json"
            state_path.write_text('{"items": []}', encoding="utf-8")
            original_replace = Path.replace
            calls = 0

            def flaky_replace(self, target):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError("locked")
                return original_replace(self, target)

            with patch.object(Path, "replace", flaky_replace):
                _write_state({"items": [{"status": "needs_review"}]}, state_path)

            progress = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(2, calls)
        self.assertEqual("needs_review", progress["items"][0]["status"])


def _write_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "2026"
    sheet.append(["Date", "Name", "Director", "Year", "Rating", "Quality", "Comment"])
    sheet.append(["2026-01-01", "Excel Confirm", "Director", 2026, 4.5, "1080p", "confirm"])
    sheet.append(["2026-01-02", "Excel Reject", "Director", 2026, 4.0, "1080p", "reject"])
    workbook.save(path)


def _review_item(source_row_hash: str, source_row_number: int, candidate_subject_id: str) -> dict:
    return {
        "source_row_hash": source_row_hash,
        "source_raw_id": f"raw-{source_row_number}",
        "source_file": "MOVIES.xlsx#2026",
        "source_row_number": source_row_number,
        "title": f"Excel {source_row_number}",
        "release_year": 2026,
        "match_status": "needs_review",
        "match_score": 0.75,
        "match_reasons": ["year_match_title_differs"],
        "candidate_subject_id": candidate_subject_id,
        "candidate_title": f"Candidate {source_row_number}",
        "candidate_year": 2026,
        "candidate_director": None,
        "status": "needs_review",
    }


def _detail(subject_id: str, title: str) -> DoubanMovieDetail:
    return DoubanMovieDetail(
        subject_id=subject_id,
        title=title,
        year=2026,
        directors=("Director",),
        genres=("Drama",),
        countries=("Country",),
        url=f"https://movie.douban.com/subject/{subject_id}/",
    )


if __name__ == "__main__":
    unittest.main()
