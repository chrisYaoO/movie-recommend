from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Callable

from backend.app.db.repository import ViewingHistoryRepository, ViewingHistoryRow
from backend.app.services.viewing_history_record_service import _source_row_checksum
from backend.app.services.viewing_history_sync_service import ViewingHistorySyncService, _projection_values


@dataclass(frozen=True)
class EditViewingHistoryRequest:
    watched_date: date
    rating: float
    quality: str | None = None
    comment: str | None = None


class ViewingHistoryManagementService:
    def __init__(
        self,
        repository: ViewingHistoryRepository,
        syncer: ViewingHistorySyncService,
        restore_candidate: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.syncer = syncer
        self.restore_candidate = restore_candidate

    def list(self, limit: int = 50, offset: int = 0, year: int | None = None, descending: bool = True) -> dict:
        return {
            "items": [
                self.to_response(row)
                for row in self.repository.find_active_viewing_history(limit, offset, year, descending)
            ],
            "total": self.repository.count_active_viewing_history(year),
            "years": self.repository.find_active_viewing_history_years(),
        }

    def edit(self, history_id: str, request: EditViewingHistoryRequest) -> dict:
        self._validate(request)
        row = self.repository.find_viewing_history(history_id)
        if row is None:
            raise KeyError(f"viewing history not found: {history_id}")
        edited = replace(
            row,
            watched_date=request.watched_date,
            user_rating=request.rating,
            quality=request.quality,
            comment=request.comment,
        )
        checksum = _source_row_checksum(_projection_values(edited))
        if not self.repository.update_viewing_history_and_enqueue(
            history_id,
            request.watched_date,
            request.rating,
            request.quality,
            request.comment,
            checksum,
        ):
            raise KeyError(f"viewing history not found: {history_id}")
        self.syncer.sync_pending()
        return self.to_response(self.repository.find_viewing_history(history_id))

    def delete(self, history_id: str) -> dict:
        row = self.repository.find_viewing_history(history_id, include_deleted=True)
        if row is None:
            raise KeyError(f"viewing history not found: {history_id}")
        self.repository.soft_delete_viewing_history_and_enqueue(history_id)
        self.syncer.sync_pending()
        if row.movie_id and self.restore_candidate:
            self.restore_candidate(row.movie_id)
        current = self.repository.find_viewing_history(history_id, include_deleted=True)
        return {"id": history_id, "deleted": True, "sync_state": _sync_state(current)}

    def retry(self, history_id: str) -> dict:
        if not self.repository.retry_sheet_sync(history_id):
            raise KeyError(f"pending Sheet sync not found: {history_id}")
        self.syncer.sync_pending()
        row = self.repository.find_viewing_history(history_id, include_deleted=True)
        return {"id": history_id, "sync_state": _sync_state(row)}

    def sync_health(self) -> dict:
        return self.syncer.health()

    @staticmethod
    def to_response(row: ViewingHistoryRow | None) -> dict:
        if row is None:
            raise KeyError("viewing history not found")
        payload = asdict(row)
        payload["watched_date"] = row.watched_date.isoformat() if row.watched_date else None
        payload["sync_state"] = _sync_state(row)
        payload.pop("deleted_at", None)
        payload.pop("sync_operation", None)
        return payload

    @staticmethod
    def _validate(request: EditViewingHistoryRequest) -> None:
        if not 0 <= request.rating <= 5:
            raise ValueError("rating must be between 0 and 5")
        if request.quality and len(request.quality) > 100:
            raise ValueError("quality must be 100 characters or fewer")
        if request.comment and len(request.comment) > 2000:
            raise ValueError("comment must be 2000 characters or fewer")


def _sync_state(row: ViewingHistoryRow | None) -> str:
    if row is None or row.sync_operation is None:
        return "synced"
    return "failed" if row.sync_attempts else "pending"
