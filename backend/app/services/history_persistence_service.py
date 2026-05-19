from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.app.db.repository import PersistViewingHistoryResult, ViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput
from backend.app.services.metadata_service import DoubanDetailAdapter

PersistConfirmedHistoryStatus = Literal["existing", "fetched", "failed"]


@dataclass(frozen=True)
class PersistConfirmedHistoryItemResult:
    source_row_hash: str | None
    douban_subject_id: str
    status: PersistConfirmedHistoryStatus
    movie_id: str | None = None
    viewing_history_id: str | None = None
    title: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PersistConfirmedHistoryRunResult:
    items: tuple[PersistConfirmedHistoryItemResult, ...]

    @property
    def persisted_count(self) -> int:
        return sum(1 for item in self.items if item.status in ("existing", "fetched"))

    @property
    def existing_count(self) -> int:
        return self._count("existing")

    @property
    def fetched_count(self) -> int:
        return self._count("fetched")

    @property
    def failed_count(self) -> int:
        return self._count("failed")

    def _count(self, status: PersistConfirmedHistoryStatus) -> int:
        return sum(1 for item in self.items if item.status == status)


def persist_confirmed_viewing_history(
    confirmed_inputs: list[ConfirmedViewingHistoryInput],
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
) -> PersistConfirmedHistoryRunResult:
    results: list[PersistConfirmedHistoryItemResult] = []

    for confirmed in confirmed_inputs:
        try:
            existing_movie = repository.find_movie_by_subject_id(confirmed.douban_subject_id)
            if existing_movie is not None:
                history = repository.upsert_viewing_history(confirmed, existing_movie.id)
                persisted = PersistViewingHistoryResult(movie=existing_movie, history=history)
                results.append(_to_success_result(confirmed, persisted, "existing", existing_movie.title))
                continue

            detail = detail_adapter.fetch(confirmed.douban_subject_id)
            persisted = repository.persist_confirmed_viewing_history(confirmed, detail)
            results.append(_to_success_result(confirmed, persisted, "fetched", detail.title))
        except Exception as exc:
            results.append(
                PersistConfirmedHistoryItemResult(
                    source_row_hash=confirmed.source_row_hash,
                    douban_subject_id=confirmed.douban_subject_id,
                    status="failed",
                    error=str(exc),
                )
            )

    return PersistConfirmedHistoryRunResult(items=tuple(results))


def _to_success_result(
    confirmed: ConfirmedViewingHistoryInput,
    persisted: PersistViewingHistoryResult,
    status: PersistConfirmedHistoryStatus,
    title: str,
) -> PersistConfirmedHistoryItemResult:
    return PersistConfirmedHistoryItemResult(
        source_row_hash=confirmed.source_row_hash,
        douban_subject_id=confirmed.douban_subject_id,
        status=status,
        movie_id=persisted.movie.id,
        viewing_history_id=persisted.history.id,
        title=title,
    )
