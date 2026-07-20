from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail


@dataclass(frozen=True)
class PersistedMovie:
    id: str
    douban_subject_id: str
    title: str
    year: int | None = None
    directors: tuple[str, ...] = ()
    poster_url: str | None = None


@dataclass(frozen=True)
class PersistedViewingHistory:
    id: str
    douban_subject_id: str
    movie_id: str | None
    source_row_checksum: str


@dataclass(frozen=True)
class PersistViewingHistoryResult:
    movie: PersistedMovie
    history: PersistedViewingHistory


@dataclass(frozen=True)
class SheetSyncTask:
    history_id: str
    operation: str
    attempts: int
    last_error: str | None
    updated_at: datetime | str


@dataclass(frozen=True)
class ViewingHistoryRow:
    id: str
    movie_id: str | None
    douban_subject_id: str
    title: str
    year: int | None
    directors: tuple[str, ...]
    poster_url: str | None
    watched_date: date | None
    user_rating: float
    quality: str | None
    comment: str | None
    source_row_checksum: str
    source_sheet_name: str
    source_row_number: int
    deleted_at: datetime | str | None
    sync_operation: str | None = None
    sync_attempts: int = 0
    sync_error: str | None = None


@dataclass(frozen=True)
class CandidateSubjectQueueItem:
    douban_subject_id: str
    source_type: str
    source_ref: str
    source_subject_id: str | None
    source_label: str | None
    status: str


class ViewingHistoryRepository(Protocol):
    def initialize_schema(self) -> None:
        pass

    def close(self) -> None:
        pass

    def persist_confirmed_viewing_history(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        detail: DoubanMovieDetail,
    ) -> PersistViewingHistoryResult:
        pass

    def find_movie_by_subject_id(self, subject_id: str) -> PersistedMovie | None:
        pass

    def find_watched_movies(self, limit: int | None = None) -> list[PersistedMovie]:
        pass

    def find_history_subject_ids_missing_movies(self, limit: int | None = None) -> list[str]:
        pass

    def backfill_viewing_history_movie_id(self, douban_subject_id: str, movie_id: str) -> int:
        pass

    def find_unprocessed_watched_movies_for_history_recommendations(
        self,
        limit: int | None = None,
    ) -> list[PersistedMovie]:
        pass

    def count_unprocessed_watched_movies_for_history_recommendations(self) -> int:
        pass

    def mark_history_recommendation_discovery_status(
        self,
        subject_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        pass

    def upsert_movie_detail(self, detail: DoubanMovieDetail) -> PersistedMovie:
        pass

    def upsert_viewing_history(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        movie_id: str | None = None,
    ) -> PersistedViewingHistory:
        pass

    def save_viewing_history_and_enqueue(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        movie_id: str | None = None,
    ) -> PersistedViewingHistory:
        pass

    def update_viewing_history_and_enqueue(
        self,
        history_id: str,
        watched_date: date,
        user_rating: float,
        quality: str | None,
        comment: str | None,
        source_row_checksum: str,
    ) -> bool:
        pass

    def soft_delete_viewing_history_and_enqueue(self, history_id: str) -> bool:
        pass

    def find_pending_sheet_sync_tasks(self, limit: int = 50) -> list[SheetSyncTask]:
        pass

    def find_viewing_history(self, history_id: str, include_deleted: bool = False) -> ViewingHistoryRow | None:
        pass

    def find_active_viewing_history(
        self,
        limit: int = 50,
        offset: int = 0,
        year: int | None = None,
        descending: bool = True,
    ) -> list[ViewingHistoryRow]:
        pass

    def count_active_viewing_history(self, year: int | None = None) -> int:
        pass

    def find_active_viewing_history_years(self) -> list[int]:
        pass

    def complete_sheet_sync(
        self,
        history_id: str,
        expected_updated_at: datetime | str,
        sheet_name: str | None = None,
        row_number: int | None = None,
    ) -> None:
        pass

    def fail_sheet_sync(self, history_id: str, expected_updated_at: datetime | str, error: str) -> None:
        pass

    def retry_sheet_sync(self, history_id: str) -> bool:
        pass

    def sheet_sync_health(self) -> dict[str, int | str | None]:
        pass

    def upsert_candidate_subject(
        self,
        subject_id: str,
        source_type: str,
        source_ref: str,
        source_subject_id: str | None = None,
        source_label: str | None = None,
    ) -> bool:
        pass

    def find_pending_candidate_subjects(self, limit: int | None = None) -> list[CandidateSubjectQueueItem]:
        pass

    def find_candidate_subjects_by_status(
        self,
        status: str,
        limit: int | None = None,
    ) -> list[CandidateSubjectQueueItem]:
        pass

    def find_candidate_subjects_by_statuses(
        self,
        statuses: tuple[str, ...],
        limit: int | None = None,
    ) -> list[CandidateSubjectQueueItem]:
        pass

    def count_candidate_subjects_by_status(self, status: str) -> int:
        pass

    def mark_candidate_subject_status(self, subject_id: str, status: str, error: str | None = None) -> None:
        pass

    def upsert_candidate_pool_entry(
        self,
        movie_id: str,
        source_type: str,
        source_ref: str,
        source_label: str | None = None,
    ) -> bool:
        pass

    def backfill_candidate_source_labels_from_movies(self) -> int:
        pass


