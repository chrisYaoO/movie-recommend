from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail


@dataclass(frozen=True)
class PersistedMovie:
    id: str
    douban_subject_id: str
    title: str


@dataclass(frozen=True)
class PersistedViewingHistory:
    id: str
    movie_id: str
    source_row_hash: str


@dataclass(frozen=True)
class PersistViewingHistoryResult:
    movie: PersistedMovie
    history: PersistedViewingHistory


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

    def upsert_movie_detail(self, detail: DoubanMovieDetail) -> PersistedMovie:
        pass

    def upsert_viewing_history(
        self,
        confirmed: ConfirmedViewingHistoryInput,
        movie_id: str,
    ) -> PersistedViewingHistory:
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

    def mark_candidate_subject_status(self, subject_id: str, status: str, error: str | None = None) -> None:
        pass

    def upsert_candidate_pool_entry(self, movie_id: str, source_type: str, source_ref: str) -> bool:
        pass
