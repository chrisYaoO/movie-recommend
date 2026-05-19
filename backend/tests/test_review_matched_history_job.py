import json
from contextlib import redirect_stdout
from io import StringIO
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import Workbook

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail
from backend.app.services.import_service import InMemoryViewingHistoryRawRepository, ViewingHistoryImportService
from backend.app.services.metadata_service import FakeDoubanDetailAdapter
from jobs.review_matched_history import (
    REVIEW_CONFIRMED_STATUS,
    REVIEW_REJECTED_STATUS,
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
