from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable
from uuid import uuid4

from backend.app.config import resolve_postgres_dsn
from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, DoubanMovieDetail, ViewingHistoryCandidate
from backend.app.services.import_service import InMemoryViewingHistoryRawRepository, ViewingHistoryImportService
from backend.app.services.matching_service import (
    DoubanHttpSearchAdapter,
    DoubanSearchAdapter,
    build_douban_match_inputs,
    run_search_match_job,
)
from backend.app.services.metadata_service import (
    DEFAULT_CHROME_BINARY_PATH,
    DoubanDetailAdapter,
    DoubanHttpDetailAdapter,
    DoubanSeleniumDetailAdapter,
)
from jobs.import_auto_matched_history import _replace_with_retries


REVIEW_PENDING_STATUS = "needs_review"
REVIEW_CONFIRMED_PENDING_STATUS = "review_confirmed_pending"
REVIEW_CONFIRMED_STATUS = "review_confirmed_persisted"
REVIEW_REJECTED_STATUS = "review_rejected"
NO_MATCH_STATUS = "no_match"
MANUAL_ID_PERSISTED_STATUS = "manual_id_persisted"
MANUAL_ID_REJECTED_STATUS = "manual_id_rejected"
MANUAL_ID_SOURCE_STATUSES = {REVIEW_REJECTED_STATUS, NO_MATCH_STATUS}
TERMINAL_STATUSES = {
    "auto_matched_persisted",
    REVIEW_CONFIRMED_STATUS,
    REVIEW_REJECTED_STATUS,
    MANUAL_ID_PERSISTED_STATUS,
    MANUAL_ID_REJECTED_STATUS,
}


@dataclass(frozen=True)
class ManualReviewSummary:
    review_candidate_count: int
    already_finished_count: int
    confirmed_count: int
    rejected_count: int
    failed_count: int
    remaining_count: int


@dataclass(frozen=True)
class CandidateIndexes:
    by_checksum: dict[str, ViewingHistoryCandidate]
    by_source_row: dict[tuple[str, int], ViewingHistoryCandidate]
    by_title_year: dict[tuple[str, int | None], ViewingHistoryCandidate]


def review_matched_history(
    excel_path: str | Path,
    state_path: str | Path,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
    input_func: Callable[[str], str] = input,
    limit: int | None = None,
) -> ManualReviewSummary:
    state = _load_state(state_path)
    candidate_indexes = _load_candidate_indexes(excel_path)
    terminal_items_by_checksum = _terminal_items_by_checksum(state)
    review_items = [
        item
        for item in state.get("items", [])
        if item.get("status") == REVIEW_PENDING_STATUS and item.get("candidate_subject_id")
    ]

    confirmed_count = 0
    rejected_count = 0
    failed_count = 0
    attempted_count = 0
    review_limit = len(review_items) if limit is None else min(limit, len(review_items))
    print(f"[review] pending={len(review_items)}, this_run={review_limit}")

    for item in review_items:
        if limit is not None and attempted_count >= limit:
            break
        candidate = _resolve_candidate_for_item(item, candidate_indexes, "review")
        if candidate is None:
            item["review_error"] = "source row checksum not found in Excel import"
            item["review_status"] = "failed"
            failed_count += 1
            _write_state(state, state_path)
            continue
        if _mark_duplicate_from_terminal_item(item, terminal_items_by_checksum):
            _write_state(state, state_path)
            continue

        attempted_count += 1
        _print_review_item(item, candidate, attempted_count, review_limit)
        answer = input_func("Enter=queue confirm, 1=reject, q=quit and persist queued: ").strip().casefold()
        if answer == "q":
            break
        if answer == "1":
            item["status"] = REVIEW_REJECTED_STATUS
            item["review_decision"] = "rejected"
            terminal_items_by_checksum[item["source_row_checksum"]] = item
            rejected_count += 1
            _write_state(state, state_path)
            continue
        if answer:
            print("Unrecognized input; leaving this row for later review.")
            continue

        item.update(
            {
                "status": REVIEW_CONFIRMED_PENDING_STATUS,
                "review_decision": "confirmed",
            }
        )
        terminal_items_by_checksum[item["source_row_checksum"]] = item
        _write_state(state, state_path)
        print("Queued for persistence after review exits.")

    batch = _persist_pending_confirmed_reviews(state, state_path, candidate_indexes, detail_adapter, repository)
    confirmed_count += batch.confirmed_count
    failed_count += batch.failed_count

    remaining_count = sum(
        1
        for item in state.get("items", [])
        if item.get("status") == REVIEW_PENDING_STATUS and item.get("candidate_subject_id")
    )
    return ManualReviewSummary(
        review_candidate_count=len(review_items),
        already_finished_count=sum(
            1
            for item in state.get("items", [])
            if item.get("status") in {REVIEW_CONFIRMED_PENDING_STATUS, REVIEW_CONFIRMED_STATUS, REVIEW_REJECTED_STATUS}
        ),
        confirmed_count=confirmed_count,
        rejected_count=rejected_count,
        failed_count=failed_count,
        remaining_count=remaining_count,
    )


def resolve_rejected_or_no_match_history(
    excel_path: str | Path,
    state_path: str | Path,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
    search_adapter: DoubanSearchAdapter | None = None,
    input_func: Callable[[str], str] = input,
    limit: int | None = None,
) -> ManualReviewSummary:
    state = _load_state(state_path)
    candidate_indexes = _load_candidate_indexes(excel_path)
    terminal_items_by_checksum = _terminal_items_by_checksum(state)
    review_items = [
        item
        for item in state.get("items", [])
        if item.get("status") in MANUAL_ID_SOURCE_STATUSES and _progress_row_checksum(item)
    ]

    confirmed_count = 0
    rejected_count = 0
    failed_count = 0
    attempted_count = 0
    review_limit = len(review_items) if limit is None else min(limit, len(review_items))
    print(f"[manual-id] pending={len(review_items)}, this_run={review_limit}")

    should_quit = False
    for item in review_items:
        if should_quit or (limit is not None and attempted_count >= limit):
            break
        candidate = _resolve_candidate_for_item(item, candidate_indexes, "manual_id")
        if candidate is None:
            item["manual_id_error"] = "source row checksum not found in Excel import"
            item["manual_id_status"] = "failed"
            failed_count += 1
            _write_state(state, state_path)
            continue
        if _mark_duplicate_from_terminal_item(item, terminal_items_by_checksum):
            _write_state(state, state_path)
            continue

        attempted_count += 1
        _print_manual_id_item(item, candidate, attempted_count, review_limit)
        while True:
            answer = input_func("Subject id, Enter=skip, a=search again, x=discard, q=quit: ").strip()
            lowered = answer.casefold()
            if lowered == "q":
                should_quit = True
                break
            if not answer:
                break
            if lowered == "a":
                if search_adapter is None:
                    print("Search retry is unavailable in this context.")
                    continue
                try:
                    search_result = _retry_search_for_manual_item(
                        item,
                        candidate,
                        search_adapter,
                        detail_adapter,
                        repository,
                    )
                except Exception as exc:
                    item["manual_id_status"] = "failed"
                    item["manual_id_error"] = str(exc)
                    failed_count += 1
                    _write_state(state, state_path)
                    print(f"Search retry failed: {exc}")
                    break
                _write_state(state, state_path)
                if search_result == "auto_matched_persisted":
                    confirmed_count += 1
                print(f"Fresh Douban search status: {item.get('match_status')}, progress status: {item.get('status')}")
                _print_retry_search_candidate(item)
                break
            if lowered == "x":
                item.update(
                    {
                        "status": MANUAL_ID_REJECTED_STATUS,
                        "manual_id_decision": "discarded_without_subject_id",
                    }
                )
                rejected_count += 1
                _write_state(state, state_path)
                print("Discarded.")
                break

            subject_id = _extract_subject_id(answer)
            if subject_id is None:
                print("Unrecognized subject id. Paste digits or a Douban subject URL.")
                continue

            try:
                detail = detail_adapter.fetch(subject_id)
            except Exception as exc:
                item["manual_id_status"] = "failed"
                item["manual_id_error"] = str(exc)
                failed_count += 1
                _write_state(state, state_path)
                print(f"Failed to fetch subject {subject_id}: {exc}")
                break

            _print_manual_id_detail(detail)
            decision = input_func("Enter=confirm, 1=discard, b=back, q=quit: ").strip().casefold()
            if decision == "q":
                should_quit = True
                break
            if decision == "b":
                continue
            if decision == "1":
                item.update(
                    {
                        "status": MANUAL_ID_REJECTED_STATUS,
                        "manual_id_decision": "discarded_after_detail",
                        "manual_id_subject_id": subject_id,
                        "manual_id_title": detail.title,
                    }
                )
                rejected_count += 1
                _write_state(state, state_path)
                print("Discarded.")
                break
            if decision:
                print("Unrecognized input; leaving this row for later.")
                break

            confirmed = _to_manual_confirmed_input(candidate, subject_id)
            try:
                persisted = _persist_confirmed_review_with_detail(confirmed, detail, repository)
            except Exception as exc:
                item["manual_id_status"] = "failed"
                item["manual_id_error"] = str(exc)
                failed_count += 1
                _write_state(state, state_path)
                print(f"Failed to persist subject {subject_id}: {exc}")
                break

            item.update(
                {
                    "status": MANUAL_ID_PERSISTED_STATUS,
                    "manual_id_decision": "confirmed",
                    "manual_id_subject_id": subject_id,
                    "candidate_subject_id": subject_id,
                    "candidate_title": detail.title,
                    "candidate_year": detail.year,
                    "candidate_director": ", ".join(detail.directors) if detail.directors else None,
                    "persistence_status": "existing" if persisted.existing_movie else "fetched",
                    "movie_id": persisted.movie_id,
                    "viewing_history_id": persisted.viewing_history_id,
                    "persisted_title": persisted.title,
                }
            )
            confirmed_count += 1
            _write_state(state, state_path)
            print(f"Persisted {item.get('source_sheet_name')}:{item.get('source_row_number')}: {persisted.title}")
            break

    remaining_count = sum(
        1
        for item in state.get("items", [])
        if item.get("status") in MANUAL_ID_SOURCE_STATUSES and _progress_row_checksum(item)
    )
    return ManualReviewSummary(
        review_candidate_count=len(review_items),
        already_finished_count=sum(
            1
            for item in state.get("items", [])
            if item.get("status") in {MANUAL_ID_PERSISTED_STATUS, MANUAL_ID_REJECTED_STATUS}
        ),
        confirmed_count=confirmed_count,
        rejected_count=rejected_count,
        failed_count=failed_count,
        remaining_count=remaining_count,
    )


def batch_search_rejected_or_no_match_history(
    excel_path: str | Path,
    state_path: str | Path,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
    search_adapter: DoubanSearchAdapter,
    limit: int | None = None,
) -> ManualReviewSummary:
    state = _load_state(state_path)
    candidate_indexes = _load_candidate_indexes(excel_path)
    terminal_items_by_checksum = _terminal_items_by_checksum(state)
    review_items = [
        item
        for item in state.get("items", [])
        if item.get("status") in MANUAL_ID_SOURCE_STATUSES and _progress_row_checksum(item)
    ]

    confirmed_count = 0
    failed_count = 0
    attempted_count = 0
    review_limit = len(review_items) if limit is None else min(limit, len(review_items))
    print(f"[batch-search-again] pending={len(review_items)}, this_run={review_limit}")

    for item in review_items:
        if limit is not None and attempted_count >= limit:
            break
        candidate = _resolve_candidate_for_item(item, candidate_indexes, "manual_id")
        if candidate is None:
            item["manual_id_error"] = "source row checksum not found in Excel import"
            item["manual_id_status"] = "failed"
            failed_count += 1
            _write_state(state, state_path)
            continue
        if _mark_duplicate_from_terminal_item(item, terminal_items_by_checksum):
            _write_state(state, state_path)
            continue

        attempted_count += 1
        print()
        print(f"[batch-search-again] {attempted_count}/{review_limit}")
        print(f"{candidate.source_sheet_name}:{candidate.source_row_number}")
        print(f"Excel : {candidate.title} ({candidate.release_year or '-'})")
        try:
            search_result = _retry_search_for_manual_item(item, candidate, search_adapter, detail_adapter, repository)
        except Exception as exc:
            item["manual_id_status"] = "failed"
            item["manual_id_error"] = str(exc)
            failed_count += 1
            _write_state(state, state_path)
            print(f"Fresh Douban search failed: {exc}")
            continue

        if search_result == "auto_matched_persisted":
            confirmed_count += 1
            terminal_items_by_checksum[item["source_row_checksum"]] = item
        _write_state(state, state_path)
        print(f"Fresh Douban search status: {item.get('match_status')}, progress status: {item.get('status')}")
        _print_retry_search_candidate(item)

    remaining_count = sum(
        1
        for item in state.get("items", [])
        if item.get("status") in MANUAL_ID_SOURCE_STATUSES and _progress_row_checksum(item)
    )
    return ManualReviewSummary(
        review_candidate_count=len(review_items),
        already_finished_count=sum(
            1
            for item in state.get("items", [])
            if item.get("status") in {MANUAL_ID_PERSISTED_STATUS, MANUAL_ID_REJECTED_STATUS, "auto_matched_persisted"}
        ),
        confirmed_count=confirmed_count,
        rejected_count=0,
        failed_count=failed_count,
        remaining_count=remaining_count,
    )


def _retry_search_for_manual_item(
    item: dict[str, Any],
    candidate: ViewingHistoryCandidate,
    search_adapter: DoubanSearchAdapter,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
) -> str:
    match = run_search_match_job(build_douban_match_inputs([candidate]).inputs, search_adapter).candidates[0]
    item.update(
        {
            "match_status": match.status.value,
            "match_score": match.match_score,
            "match_reasons": list(match.match_reasons),
            "candidate_subject_id": match.candidate_subject_id,
            "candidate_title": match.candidate_title,
            "candidate_year": match.candidate_year,
            "candidate_director": match.candidate_director,
        }
    )
    if match.status.value == "auto_matched":
        confirmed = _to_confirmed_input(candidate, item)
        persisted = _persist_confirmed_review(confirmed, detail_adapter, repository)
        item.update(
            {
                "status": "auto_matched_persisted",
                "persistence_status": "existing" if persisted.existing_movie else "fetched",
                "movie_id": persisted.movie_id,
                "viewing_history_id": persisted.viewing_history_id,
                "persisted_title": persisted.title,
            }
        )
        print(f"Persisted {item.get('source_sheet_name')}:{item.get('source_row_number')}: {persisted.title}")
        return "auto_matched_persisted"
    if match.status.value == REVIEW_PENDING_STATUS:
        item["status"] = REVIEW_PENDING_STATUS
        item.pop("manual_id_status", None)
        item.pop("manual_id_error", None)
        return REVIEW_PENDING_STATUS

    item["status"] = NO_MATCH_STATUS
    return NO_MATCH_STATUS


def _print_retry_search_candidate(item: dict[str, Any]) -> None:
    print(f"candidate_subject_id: {item.get('candidate_subject_id') or '-'}")
    print(f"candidate_title: {item.get('candidate_title') or '-'}")
    print(f"candidate_year: {item.get('candidate_year') or '-'}")
    print(f"match_score: {item.get('match_score')}")
    print(f"match_reasons: {', '.join(item.get('match_reasons') or []) or '-'}")


def _load_candidate_indexes(excel_path: str | Path) -> CandidateIndexes:
    import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
    import_service.import_excel(excel_path)
    mapping = import_service.to_viewing_history_candidates()
    by_checksum = {
        candidate.source_row_checksum: candidate
        for candidate in mapping.candidates
        if candidate.source_row_checksum is not None
    }
    by_source_row = {
        (candidate.source_sheet_name, candidate.source_row_number): candidate
        for candidate in mapping.candidates
    }
    by_title_year: dict[tuple[str, int | None], ViewingHistoryCandidate] = {}
    duplicate_title_year_keys: set[tuple[str, int | None]] = set()
    for candidate in mapping.candidates:
        key = _candidate_title_year_key(candidate.title, candidate.release_year)
        if key in by_title_year:
            duplicate_title_year_keys.add(key)
        else:
            by_title_year[key] = candidate
    for key in duplicate_title_year_keys:
        by_title_year.pop(key, None)
    return CandidateIndexes(by_checksum=by_checksum, by_source_row=by_source_row, by_title_year=by_title_year)


def _resolve_candidate_for_item(
    item: dict[str, Any],
    candidate_indexes: CandidateIndexes,
    status_prefix: str,
) -> ViewingHistoryCandidate | None:
    source_row_checksum = _progress_row_checksum(item)
    if source_row_checksum:
        candidate = candidate_indexes.by_checksum.get(source_row_checksum)
        if candidate is not None:
            return candidate

    source_sheet_name = _progress_source_sheet_name(item)
    source_row_number = item.get("source_row_number")
    if not source_sheet_name or not isinstance(source_row_number, int):
        return None

    candidate = candidate_indexes.by_source_row.get((source_sheet_name, source_row_number))
    if candidate is None:
        candidate = candidate_indexes.by_title_year.get(
            _candidate_title_year_key(item.get("title"), item.get("release_year"))
        )
    if candidate is None:
        return None

    item["source_row_checksum"] = candidate.source_row_checksum
    item["source_raw_id"] = candidate.source_raw_id
    item["source_sheet_name"] = candidate.source_sheet_name
    item["source_row_number"] = candidate.source_row_number
    item["title"] = candidate.title
    item["release_year"] = candidate.release_year
    item.pop(f"{status_prefix}_error", None)
    if item.get(f"{status_prefix}_status") == "failed":
        item.pop(f"{status_prefix}_status", None)
    return candidate


def _terminal_items_by_checksum(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    terminal_items: dict[str, dict[str, Any]] = {}
    for item in state.get("items", []):
        source_row_checksum = _progress_row_checksum(item)
        if item.get("status") in TERMINAL_STATUSES and source_row_checksum:
            terminal_items.setdefault(source_row_checksum, item)
    return terminal_items


def _progress_row_checksum(item: dict[str, Any]) -> str | None:
    return item.get("source_row_checksum") or item.get("source_row_hash")


def _progress_source_sheet_name(item: dict[str, Any]) -> str | None:
    source_sheet_name = item.get("source_sheet_name")
    if source_sheet_name:
        return str(source_sheet_name)
    old_source_file = item.get("source_file")
    if not old_source_file:
        return None
    source_text = str(old_source_file)
    if "#" in source_text:
        return source_text.rsplit("#", 1)[1]
    return source_text


def _mark_duplicate_from_terminal_item(
    item: dict[str, Any],
    terminal_items_by_checksum: dict[str, dict[str, Any]],
) -> bool:
    source_row_checksum = _progress_row_checksum(item)
    if not source_row_checksum:
        return False
    terminal_item = terminal_items_by_checksum.get(source_row_checksum)
    if terminal_item is None or terminal_item is item:
        return False

    item["status"] = terminal_item.get("status")
    item["duplicate_of_source_row_checksum"] = source_row_checksum
    for key in (
        "review_decision",
        "manual_id_decision",
        "persistence_status",
        "movie_id",
        "viewing_history_id",
        "persisted_title",
    ):
        if key in terminal_item:
            item[key] = terminal_item[key]
    item.pop("review_status", None)
    item.pop("review_error", None)
    item.pop("manual_id_status", None)
    item.pop("manual_id_error", None)
    return True


def _candidate_title_year_key(title: Any, release_year: Any) -> tuple[str, int | None]:
    normalized_title = str(title or "").strip().casefold()
    year = release_year if isinstance(release_year, int) else None
    return (normalized_title, year)


@dataclass(frozen=True)
class PendingPersistSummary:
    confirmed_count: int
    failed_count: int


def _persist_pending_confirmed_reviews(
    state: dict[str, Any],
    state_path: str | Path,
    candidate_indexes: CandidateIndexes,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
) -> PendingPersistSummary:
    confirmed_count = 0
    failed_count = 0
    terminal_items_by_checksum = _terminal_items_by_checksum(state)
    pending_items = [
        item
        for item in state.get("items", [])
        if item.get("status") == REVIEW_CONFIRMED_PENDING_STATUS and item.get("candidate_subject_id")
    ]
    if pending_items:
        print()
        print(f"Persisting {len(pending_items)} confirmed review item(s)...")

    for item in pending_items:
        candidate = _resolve_candidate_for_item(item, candidate_indexes, "review")
        if candidate is None:
            item["review_status"] = "failed"
            item["review_error"] = "source row checksum not found in Excel import"
            failed_count += 1
            _write_state(state, state_path)
            continue
        if _mark_duplicate_from_terminal_item(item, terminal_items_by_checksum):
            _write_state(state, state_path)
            continue

        confirmed = _to_confirmed_input(candidate, item)
        try:
            persisted = _persist_confirmed_review(confirmed, detail_adapter, repository)
        except Exception as exc:
            item["review_status"] = "failed"
            item["review_error"] = str(exc)
            failed_count += 1
            _write_state(state, state_path)
            print(f"Failed {item.get('source_sheet_name')}:{item.get('source_row_number')}: {exc}")
            continue

        item.update(
            {
                "status": REVIEW_CONFIRMED_STATUS,
                "persistence_status": "existing" if persisted.existing_movie else "fetched",
                "movie_id": persisted.movie_id,
                "viewing_history_id": persisted.viewing_history_id,
                "persisted_title": persisted.title,
            }
        )
        confirmed_count += 1
        terminal_items_by_checksum[item["source_row_checksum"]] = item
        _write_state(state, state_path)
        print(f"Persisted {item.get('source_sheet_name')}:{item.get('source_row_number')}: {persisted.title}")

    return PendingPersistSummary(confirmed_count=confirmed_count, failed_count=failed_count)


def _to_confirmed_input(candidate: ViewingHistoryCandidate, item: dict[str, Any]) -> ConfirmedViewingHistoryInput:
    subject_id = item.get("candidate_subject_id")
    if not subject_id:
        raise ValueError("review item has no candidate_subject_id")
    return ConfirmedViewingHistoryInput(
        source_raw_id=candidate.source_raw_id,
        source_sheet_name=candidate.source_sheet_name,
        source_row_number=candidate.source_row_number,
        douban_subject_id=subject_id,
        watched_date=candidate.watched_date,
        user_rating=candidate.user_rating,
        source_row_checksum=candidate.source_row_checksum,
        quality=candidate.quality,
        comment=candidate.comment,
    )


def _to_manual_confirmed_input(candidate: ViewingHistoryCandidate, subject_id: str) -> ConfirmedViewingHistoryInput:
    return ConfirmedViewingHistoryInput(
        source_raw_id=candidate.source_raw_id,
        source_sheet_name=candidate.source_sheet_name,
        source_row_number=candidate.source_row_number,
        douban_subject_id=subject_id,
        watched_date=candidate.watched_date,
        user_rating=candidate.user_rating,
        source_row_checksum=candidate.source_row_checksum,
        quality=candidate.quality,
        comment=candidate.comment,
    )


def _print_review_item(
    item: dict[str, Any],
    candidate: ViewingHistoryCandidate,
    current_index: int,
    total_count: int,
) -> None:
    print()
    print(f"[review] {current_index}/{total_count}")
    print(f"{candidate.source_sheet_name}:{candidate.source_row_number}")
    print(f"Excel : {candidate.title} ({candidate.release_year or '-'})")
    if candidate.director:
        print(f"Excel director: {candidate.director}")
    print(f"Douban: {item.get('candidate_title') or '-'} ({item.get('candidate_year') or '-'})")
    if item.get("candidate_director"):
        print(f"Douban director: {item.get('candidate_director')}")
    print(f"Subject: https://movie.douban.com/subject/{item.get('candidate_subject_id')}/")
    print(f"Reason : {', '.join(item.get('match_reasons') or [])}")
    print("Persist title: Douban detail title")


def _print_manual_id_item(
    item: dict[str, Any],
    candidate: ViewingHistoryCandidate,
    current_index: int,
    total_count: int,
) -> None:
    print()
    print(f"[manual-id] {current_index}/{total_count}")
    print(f"{candidate.source_sheet_name}:{candidate.source_row_number}")
    print(f"Excel : {candidate.title} ({candidate.release_year or '-'})")
    if candidate.director:
        print(f"Excel director: {candidate.director}")
    print(f"Current status: {item.get('status')}")
    if item.get("candidate_title"):
        print(f"Rejected candidate: {item.get('candidate_title')} ({item.get('candidate_year') or '-'})")
    if item.get("match_reasons"):
        print(f"Reason : {', '.join(item.get('match_reasons') or [])}")


def _print_manual_id_detail(detail: DoubanMovieDetail) -> None:
    print(f"Douban: {detail.title} ({detail.year or '-'})")
    if detail.display_title and detail.display_title != detail.title:
        print(f"Display title: {detail.display_title}")
    if detail.original_title:
        print(f"Original title: {detail.original_title}")
    if detail.aka_titles:
        print(f"AKA: {', '.join(detail.aka_titles)}")
    if detail.directors:
        print(f"Director: {', '.join(detail.directors)}")
    if detail.genres:
        print(f"Genres: {', '.join(detail.genres)}")
    print(f"Subject: https://movie.douban.com/subject/{detail.subject_id}/")


def _extract_subject_id(raw_value: str) -> str | None:
    value = raw_value.strip()
    if value.isdigit():
        return value
    match = re.search(r"/subject/(\d+)/?", value)
    if match:
        return match.group(1)
    return None


@dataclass(frozen=True)
class ReviewPersistResult:
    existing_movie: bool
    movie_id: str
    viewing_history_id: str
    title: str


def _persist_confirmed_review(
    confirmed: ConfirmedViewingHistoryInput,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
) -> ReviewPersistResult:
    existing_movie = repository.find_movie_by_subject_id(confirmed.douban_subject_id)
    if existing_movie is not None:
        history = repository.upsert_viewing_history(confirmed, existing_movie.id)
        return ReviewPersistResult(
            existing_movie=True,
            movie_id=existing_movie.id,
            viewing_history_id=history.id,
            title=existing_movie.title,
        )

    fetched_detail = detail_adapter.fetch(confirmed.douban_subject_id)
    persisted = repository.persist_confirmed_viewing_history(confirmed, fetched_detail)
    return ReviewPersistResult(
        existing_movie=False,
        movie_id=persisted.movie.id,
        viewing_history_id=persisted.history.id,
        title=persisted.movie.title,
    )


def _persist_confirmed_review_with_detail(
    confirmed: ConfirmedViewingHistoryInput,
    detail: DoubanMovieDetail,
    repository: ViewingHistoryRepository,
) -> ReviewPersistResult:
    existing_movie = repository.find_movie_by_subject_id(confirmed.douban_subject_id)
    if existing_movie is not None:
        history = repository.upsert_viewing_history(confirmed, existing_movie.id)
        return ReviewPersistResult(
            existing_movie=True,
            movie_id=existing_movie.id,
            viewing_history_id=history.id,
            title=existing_movie.title,
        )

    persisted = repository.persist_confirmed_viewing_history(confirmed, detail)
    return ReviewPersistResult(
        existing_movie=False,
        movie_id=persisted.movie.id,
        viewing_history_id=persisted.history.id,
        title=persisted.movie.title,
    )


def _load_state(state_path: str | Path) -> dict[str, Any]:
    path = Path(state_path)
    if not path.exists():
        raise FileNotFoundError(f"review state not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(state: dict[str, Any], state_path: str | Path) -> None:
    path = Path(state_path)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        _replace_with_retries(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Manually review Douban needs_review matches and persist accepted rows.")
    parser.add_argument("excel_path", help="Path to MOVIES.xlsx or another viewing-history workbook.")
    parser.add_argument("--resume-state-path", default="data/cache/import-auto-match-progress.json")
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN. Defaults to MOVIES_POSTGRES_DSN or .env.")
    parser.add_argument("--config-path", default=".env", help="Local config file path. Defaults to .env.")
    parser.add_argument("--chrome-binary-path", default=DEFAULT_CHROME_BINARY_PATH)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--detail-adapter", choices=("http", "selenium"), default="selenium")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--resolve-rejected-and-no-match",
        action="store_true",
        help="Manually enter Douban subject ids for review_rejected and no_match rows.",
    )
    parser.add_argument(
        "--batch-search-rejected-and-no-match",
        action="store_true",
        help="Run fresh Douban search for review_rejected and no_match rows without interactive prompts.",
    )
    args = parser.parse_args()

    try:
        dsn = resolve_postgres_dsn(args.dsn, args.config_path)
    except ValueError as exc:
        parser.error(str(exc))

    repository = PostgresViewingHistoryRepository(dsn)
    repository.initialize_schema()
    search_adapter = DoubanHttpSearchAdapter(timeout_seconds=args.timeout_seconds, delay_seconds=args.delay_seconds)
    if args.detail_adapter == "http":
        detail_adapter: DoubanDetailAdapter = DoubanHttpDetailAdapter(
            timeout_seconds=args.timeout_seconds,
            delay_seconds=args.delay_seconds,
        )
    else:
        detail_adapter = DoubanSeleniumDetailAdapter(
            timeout_seconds=args.timeout_seconds,
            delay_seconds=args.delay_seconds,
            chrome_binary_path=args.chrome_binary_path,
        )

    try:
        if args.batch_search_rejected_and_no_match:
            summary = batch_search_rejected_or_no_match_history(
                args.excel_path,
                args.resume_state_path,
                detail_adapter,
                repository,
                search_adapter=search_adapter,
                limit=args.limit,
            )
        elif args.resolve_rejected_and_no_match:
            summary = resolve_rejected_or_no_match_history(
                args.excel_path,
                args.resume_state_path,
                detail_adapter,
                repository,
                search_adapter=search_adapter,
                limit=args.limit,
            )
        else:
            summary = review_matched_history(
                args.excel_path,
                args.resume_state_path,
                detail_adapter,
                repository,
                limit=args.limit,
            )
    finally:
        if hasattr(detail_adapter, "close"):
            detail_adapter.close()
        repository.close()

    print(json.dumps({"summary": asdict(summary)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()



