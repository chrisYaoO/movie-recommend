import threading
import unittest

from backend.app.services.candidate_queue_service import CandidateQueueService
from backend.app.db.repository import CandidateSubjectQueueItem
from jobs.candidate_pool import CandidateQueueProcessSummary


class CandidateQueueServiceTest(unittest.TestCase):
    def test_reports_queue_counts_without_starting_selenium(self) -> None:
        state = {"pending": 3, "failed": 2}
        adapters = []
        service = CandidateQueueService(
            repository_factory=lambda: _FakeRepository(state),
            detail_adapter_factory=lambda: adapters.append(_FakeAdapter()) or adapters[-1],
        )

        status = service.status()

        self.assertEqual(3, status.pending_count)
        self.assertEqual(2, status.failed_count)
        self.assertFalse(status.processing)
        self.assertEqual([], adapters)

    def test_start_drains_pending_items_one_at_a_time_and_closes_resources(self) -> None:
        state = {"pending": 3, "failed": 0}
        adapter = _FakeAdapter()
        drained = threading.Event()

        def process_one(_repository, _adapter, limit, status_writer, queue_statuses, item_started):
            self.assertEqual(1, limit)
            self.assertIsNotNone(status_writer)
            self.assertEqual(("failed", "pending"), queue_statuses)
            item_started(_item(str(state["pending"])))
            attempted = int(state["pending"] > 0)
            state["pending"] -= attempted
            if state["pending"] == 0:
                drained.set()
            return _summary(attempted)

        service = CandidateQueueService(
            repository_factory=lambda: _FakeRepository(state),
            detail_adapter_factory=lambda: adapter,
            queue_processor=process_one,
        )

        service.start()
        self.assertTrue(drained.wait(1))
        service.stop()
        status = service.status()

        self.assertEqual(0, status.pending_count)
        self.assertEqual(3, status.processed_count)
        self.assertFalse(status.processing)
        self.assertEqual(1, adapter.close_count)

    def test_repeated_start_does_not_create_a_second_worker(self) -> None:
        state = {"pending": 1, "failed": 0}
        started = threading.Event()
        release = threading.Event()
        adapter_count = 0

        def create_adapter():
            nonlocal adapter_count
            adapter_count += 1
            return _FakeAdapter()

        def process_one(_repository, _adapter, limit, status_writer, queue_statuses, item_started):
            started.set()
            item_started(_item("blocked"))
            release.wait(1)
            state["pending"] = 0
            return _summary(1)

        service = CandidateQueueService(
            repository_factory=lambda: _FakeRepository(state),
            detail_adapter_factory=create_adapter,
            queue_processor=process_one,
        )

        first = service.start()
        self.assertTrue(started.wait(1))
        second = service.start()
        release.set()
        service.stop()

        self.assertTrue(first.processing)
        self.assertTrue(second.processing)
        self.assertEqual(1, adapter_count)

    def test_failure_stops_processing_and_blocks_retries_for_this_service_instance(self) -> None:
        state = {"pending": 2, "failed": 0}
        calls = 0
        failure_recorded = threading.Event()

        def fail_first(_repository, _adapter, limit, status_writer, queue_statuses, item_started):
            nonlocal calls
            calls += 1
            item_started(_item("failed-subject"))
            state["pending"] -= 1
            state["failed"] += 1
            failure_recorded.set()
            return _summary(1, failed_count=1, failed_subject_id="failed-subject", last_error="blocked by Douban")

        service = CandidateQueueService(
            repository_factory=lambda: _FakeRepository(state),
            detail_adapter_factory=_FakeAdapter,
            queue_processor=fail_first,
        )

        service.start()
        self.assertTrue(failure_recorded.wait(1))
        service.stop()
        failed = service.status()
        service.start()

        self.assertEqual(1, calls)
        self.assertTrue(failed.blocked_for_run)
        self.assertEqual("failed-subject", failed.current_subject_id)
        self.assertEqual("blocked by Douban", failed.failure_reason)
        self.assertEqual(2, failed.pending_count + failed.failed_count)


class _FakeRepository:
    def __init__(self, state):
        self.state = state

    def count_candidate_subjects_by_status(self, status):
        return self.state[status]

    def close(self):
        pass


class _FakeAdapter:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


def _summary(attempted_count, failed_count=0, failed_subject_id=None, last_error=None):
    return CandidateQueueProcessSummary(
        attempted_count=attempted_count,
        enriched_count=attempted_count,
        existing_movie_count=0,
        candidate_pool_inserted_count=attempted_count,
        recommendation_discovered_count=0,
        recommendation_inserted_count=0,
        failed_count=failed_count,
        failed_subject_id=failed_subject_id,
        last_error=last_error,
    )


def _item(subject_id):
    return CandidateSubjectQueueItem(
        douban_subject_id=subject_id,
        source_type="douban_recommendation",
        source_ref="recommended_from:source",
        source_subject_id="source",
        source_label="recommended from Source",
        status="pending",
    )


if __name__ == "__main__":
    unittest.main()
