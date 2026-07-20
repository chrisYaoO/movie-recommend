from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from threading import Event, Lock

from backend.app.db.repository import ViewingHistoryRepository, ViewingHistoryRow
from backend.app.services.display_text import display_person_names
from backend.app.services.google_sheets_service import GoogleSheetsHistoryService, SheetHistoryProjection


@dataclass(frozen=True)
class SheetSyncRunResult:
    processed_count: int
    succeeded_count: int
    failed_count: int
    busy: bool = False


class ViewingHistorySyncService:
    def __init__(self, repository: ViewingHistoryRepository, sheets: GoogleSheetsHistoryService) -> None:
        self.repository = repository
        self.sheets = sheets
        self.lock = Lock()
        self.stopping = Event()
        self.last_successful_run: str | None = None
        self.last_error: str | None = None

    def sync_pending(self, limit: int = 25) -> SheetSyncRunResult:
        if not self.lock.acquire(blocking=False):
            return SheetSyncRunResult(0, 0, 0, busy=True)
        succeeded = failed = 0
        try:
            tasks = self.repository.find_pending_sheet_sync_tasks(limit)
            for task in tasks:
                if self.stopping.is_set():
                    break
                row = self.repository.find_viewing_history(task.history_id, include_deleted=True)
                if row is None:
                    self.repository.fail_sheet_sync(task.history_id, task.updated_at, "local history row is missing")
                    failed += 1
                    continue
                try:
                    if task.operation == "delete":
                        self.sheets.delete_history_row(row.id, row.source_sheet_name, row.source_row_number)
                        self.repository.complete_sheet_sync(row.id, task.updated_at)
                    else:
                        if row.deleted_at is not None:
                            continue
                        location = self.sheets.upsert_history_row(
                            SheetHistoryProjection(row.id, _target_sheet(row), _projection_values(row)),
                            row.source_sheet_name,
                            row.source_row_number,
                        )
                        self.repository.complete_sheet_sync(
                            row.id,
                            task.updated_at,
                            location.sheet_name,
                            location.row_number,
                        )
                    succeeded += 1
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    self.repository.fail_sheet_sync(row.id, task.updated_at, error)
                    self.last_error = error
                    failed += 1
            if failed == 0:
                self.last_successful_run = datetime.now(timezone.utc).isoformat()
                self.last_error = None
            return SheetSyncRunResult(len(tasks), succeeded, failed)
        finally:
            self.lock.release()

    def stop(self) -> None:
        self.stopping.set()

    def health(self) -> dict[str, int | str | None | bool]:
        persisted = self.repository.sheet_sync_health()
        return {
            **persisted,
            "running": self.lock.locked(),
            "last_successful_run": self.last_successful_run,
            "last_error": self.last_error or persisted.get("last_error"),
        }


def _target_sheet(row: ViewingHistoryRow) -> str:
    return str(row.watched_date.year) if row.watched_date else row.source_sheet_name


def _projection_values(row: ViewingHistoryRow) -> list[str | float | int]:
    return [
        row.watched_date.isoformat() if row.watched_date else "",
        row.title,
        ", ".join(display_person_names(row.directors)),
        row.year or "",
        row.user_rating,
        row.quality or "",
        row.comment or "",
        row.douban_subject_id,
        _image_id(row.poster_url) or "",
    ]


def _image_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/p(\d+)\.[A-Za-z0-9]+(?:[?#].*)?$", url)
    return match.group(1) if match else None
