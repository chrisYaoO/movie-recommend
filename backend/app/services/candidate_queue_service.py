from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Event, Lock, Thread, current_thread
from typing import Callable, Protocol

from backend.app.db.repository import CandidateSubjectQueueItem, ViewingHistoryRepository
from backend.app.services.metadata_service import DoubanDetailAdapter
from jobs.candidate_pool import (
    QUEUE_STATUS_FAILED,
    QUEUE_STATUS_PENDING,
    CandidateQueueProcessSummary,
    process_candidate_queue,
)

logger = logging.getLogger("uvicorn.error")

RepositoryFactory = Callable[[], ViewingHistoryRepository]

class ClosableDetailAdapter(DoubanDetailAdapter, Protocol):
    def close(self) -> None:
        pass


DetailAdapterFactory = Callable[[], ClosableDetailAdapter]
QueueProcessor = Callable[..., CandidateQueueProcessSummary]


@dataclass(frozen=True)
class CandidateQueueStatus:
    pending_count: int
    failed_count: int
    processing: bool
    processed_count: int
    current_subject_id: str | None
    current_source_label: str | None
    current_source_ref: str | None
    blocked_for_run: bool
    failure_reason: str | None
    last_error: str | None


class CandidateQueueService:
    def __init__(
        self,
        repository_factory: RepositoryFactory,
        detail_adapter_factory: DetailAdapterFactory,
        queue_processor: QueueProcessor = process_candidate_queue,
    ) -> None:
        self._repository_factory = repository_factory
        self._detail_adapter_factory = detail_adapter_factory
        self._queue_processor = queue_processor
        self._lock = Lock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._processed_count = 0
        self._current_subject_id: str | None = None
        self._current_source_label: str | None = None
        self._current_source_ref: str | None = None
        self._blocked_for_run = False
        self._failure_reason: str | None = None
        self._last_error: str | None = None

    def status(self) -> CandidateQueueStatus:
        pending_count, failed_count = self._queue_counts()
        with self._lock:
            return CandidateQueueStatus(
                pending_count=pending_count,
                failed_count=failed_count,
                processing=self._thread is not None and self._thread.is_alive(),
                processed_count=self._processed_count,
                current_subject_id=self._current_subject_id,
                current_source_label=self._current_source_label,
                current_source_ref=self._current_source_ref,
                blocked_for_run=self._blocked_for_run,
                failure_reason=self._failure_reason,
                last_error=self._last_error,
            )

    def start(self) -> CandidateQueueStatus:
        with self._lock:
            if not self._blocked_for_run and (self._thread is None or not self._thread.is_alive()):
                pending_count, failed_count = self._queue_counts()
                if pending_count + failed_count > 0:
                    self._processed_count = 0
                    self._current_subject_id = None
                    self._current_source_label = None
                    self._current_source_ref = None
                    self._failure_reason = None
                    self._last_error = None
                    self._stop_event.clear()
                    self._thread = Thread(target=self._run, name="candidate-queue", daemon=True)
                    self._thread.start()
        return self.status()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join()

    def _run(self) -> None:
        repository = None
        detail_adapter = None
        try:
            repository = self._repository_factory()
            detail_adapter = self._detail_adapter_factory()
            while not self._stop_event.is_set():
                summary = self._queue_processor(
                    repository,
                    detail_adapter,
                    limit=1,
                    status_writer=logger.info,
                    queue_statuses=(QUEUE_STATUS_FAILED, QUEUE_STATUS_PENDING),
                    item_started=self._set_current_item,
                )
                if summary.attempted_count == 0:
                    break
                with self._lock:
                    self._processed_count += summary.attempted_count
                    if summary.failed_count > 0:
                        self._blocked_for_run = True
                        self._failure_reason = summary.last_error
                if summary.failed_count > 0:
                    break
        except Exception as exc:
            logger.exception("Candidate queue processing failed")
            with self._lock:
                self._blocked_for_run = True
                self._last_error = str(exc)
        finally:
            try:
                if detail_adapter is not None:
                    detail_adapter.close()
            finally:
                try:
                    if repository is not None:
                        repository.close()
                finally:
                    with self._lock:
                        self._thread = None

    def _queue_counts(self) -> tuple[int, int]:
        repository = self._repository_factory()
        try:
            return (
                repository.count_candidate_subjects_by_status(QUEUE_STATUS_PENDING),
                repository.count_candidate_subjects_by_status(QUEUE_STATUS_FAILED),
            )
        finally:
            repository.close()

    def _set_current_item(self, item: CandidateSubjectQueueItem) -> None:
        with self._lock:
            self._current_subject_id = item.douban_subject_id
            self._current_source_label = item.source_label
            self._current_source_ref = item.source_ref
