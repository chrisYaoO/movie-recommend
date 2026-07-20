from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail
from backend.app.services.google_sheets_service import SheetHistoryLocation
from backend.app.services.viewing_history_management_service import (
    EditViewingHistoryRequest,
    ViewingHistoryManagementService,
)
from backend.app.services.viewing_history_sync_service import ViewingHistorySyncService


class FakeSheets:
    def __init__(self, error=None):
        self.error = error
        self.upserts = []
        self.deletes = []

    def upsert_history_row(self, projection, hinted_sheet_name, hinted_row_number):
        self.upserts.append((projection, hinted_sheet_name, hinted_row_number))
        if self.error:
            raise self.error
        return SheetHistoryLocation(projection.sheet_name, 8, f"{projection.sheet_name}!A8:J8")

    def delete_history_row(self, history_id, hinted_sheet_name, hinted_row_number):
        self.deletes.append((history_id, hinted_sheet_name, hinted_row_number))
        if self.error:
            raise self.error
        return True


def seed(repository):
    movie = repository.upsert_movie_detail(
        DoubanMovieDetail(subject_id="1291561", title="千与千寻", year=2001, directors=("宫崎骏",))
    )
    history = repository.save_viewing_history_and_enqueue(
        ConfirmedViewingHistoryInput(
            source_raw_id="seed",
            source_sheet_name="2026",
            source_row_number=2,
            douban_subject_id="1291561",
            watched_date=date(2026, 1, 2),
            user_rating=4,
            source_row_checksum="old",
            history_id="history-1",
        ),
        movie.id,
    )
    task = repository.find_pending_sheet_sync_tasks()[0]
    repository.complete_sheet_sync(history.id, task.updated_at, "2026", 2)
    return history.id


def test_edit_and_delete_commit_locally_while_sheets_is_offline():
    with TemporaryDirectory() as directory:
        with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
            repository.initialize_schema()
            history_id = seed(repository)
            restored_candidates = []
            service = ViewingHistoryManagementService(
                repository,
                ViewingHistorySyncService(repository, FakeSheets(TimeoutError("offline"))),
                restored_candidates.append,
            )

            edited = service.edit(history_id, EditViewingHistoryRequest(date(2025, 12, 31), 5, "4K", "great"))
            edit_task = repository.find_pending_sheet_sync_tasks()[0]
            deleted = service.delete(history_id)
            delete_task = repository.find_pending_sheet_sync_tasks()[0]

            assert edited["watched_date"] == "2025-12-31"
            assert edited["sync_state"] == "failed"
            assert edit_task.operation == "upsert"
            assert delete_task.operation == "delete"
            assert delete_task.attempts == 1
            assert deleted == {"id": history_id, "deleted": True, "sync_state": "failed"}
            assert repository.count_active_viewing_history() == 0
            assert restored_candidates == [edited["movie_id"]]


def test_recoverable_pending_task_is_cleared_and_locator_refreshed():
    with TemporaryDirectory() as directory:
        with SQLiteViewingHistoryRepository(Path(directory) / "movies.db") as repository:
            repository.initialize_schema()
            history_id = seed(repository)
            row = repository.find_viewing_history(history_id)
            repository.update_viewing_history_and_enqueue(
                history_id, row.watched_date, 4.5, None, None, "new-checksum"
            )
            sheets = FakeSheets()
            syncer = ViewingHistorySyncService(repository, sheets)

            result = syncer.sync_pending()

            assert result.succeeded_count == 1
            assert repository.find_pending_sheet_sync_tasks() == []
            assert repository.find_viewing_history(history_id).source_row_number == 8
            assert sheets.upserts[0][0].history_id == history_id
