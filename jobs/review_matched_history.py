from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
from typing import Any, Callable

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput, ViewingHistoryCandidate
from backend.app.services.import_service import InMemoryViewingHistoryRawRepository, ViewingHistoryImportService
from backend.app.services.metadata_service import DoubanDetailAdapter, DoubanHttpDetailAdapter, DoubanSeleniumDetailAdapter
from jobs.import_auto_matched_history import resolve_postgres_dsn


REVIEW_PENDING_STATUS = "needs_review"
REVIEW_CONFIRMED_PENDING_STATUS = "review_confirmed_pending"
REVIEW_CONFIRMED_STATUS = "review_confirmed_persisted"
REVIEW_REJECTED_STATUS = "review_rejected"


@dataclass(frozen=True)
class ManualReviewSummary:
    review_candidate_count: int
    already_finished_count: int
    confirmed_count: int
    rejected_count: int
    failed_count: int
    remaining_count: int


def review_matched_history(
    excel_path: str | Path,
    state_path: str | Path,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
    input_func: Callable[[str], str] = input,
    limit: int | None = None,
) -> ManualReviewSummary:
    state = _load_state(state_path)
    candidates_by_hash = _load_candidates_by_hash(excel_path)
    review_items = [
        item
        for item in state.get("items", [])
        if item.get("status") == REVIEW_PENDING_STATUS and item.get("candidate_subject_id")
    ]

    confirmed_count = 0
    rejected_count = 0
    failed_count = 0
    attempted_count = 0

    for item in review_items:
        if limit is not None and attempted_count >= limit:
            break
        source_row_hash = item.get("source_row_hash")
        candidate = candidates_by_hash.get(source_row_hash)
        if candidate is None:
            item["review_error"] = "source row hash not found in Excel import"
            item["review_status"] = "failed"
            failed_count += 1
            _write_state(state, state_path)
            continue

        attempted_count += 1
        _print_review_item(item, candidate)
        answer = input_func("Enter=queue confirm, 1=reject, q=quit and persist queued: ").strip().casefold()
        if answer == "q":
            break
        if answer == "1":
            item["status"] = REVIEW_REJECTED_STATUS
            item["review_decision"] = "rejected"
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
        _write_state(state, state_path)
        print("Queued for persistence after review exits.")

    batch = _persist_pending_confirmed_reviews(state, state_path, candidates_by_hash, detail_adapter, repository)
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


def _load_candidates_by_hash(excel_path: str | Path) -> dict[str, ViewingHistoryCandidate]:
    import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
    import_service.import_excel(excel_path)
    mapping = import_service.to_viewing_history_candidates()
    return {
        candidate.source_row_hash: candidate
        for candidate in mapping.candidates
        if candidate.source_row_hash is not None
    }


@dataclass(frozen=True)
class PendingPersistSummary:
    confirmed_count: int
    failed_count: int


def _persist_pending_confirmed_reviews(
    state: dict[str, Any],
    state_path: str | Path,
    candidates_by_hash: dict[str, ViewingHistoryCandidate],
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
) -> PendingPersistSummary:
    confirmed_count = 0
    failed_count = 0
    pending_items = [
        item
        for item in state.get("items", [])
        if item.get("status") == REVIEW_CONFIRMED_PENDING_STATUS and item.get("candidate_subject_id")
    ]
    if pending_items:
        print()
        print(f"Persisting {len(pending_items)} confirmed review item(s)...")

    for item in pending_items:
        source_row_hash = item.get("source_row_hash")
        candidate = candidates_by_hash.get(source_row_hash)
        if candidate is None:
            item["review_status"] = "failed"
            item["review_error"] = "source row hash not found in Excel import"
            failed_count += 1
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
            print(f"Failed {item.get('source_file')}:{item.get('source_row_number')}: {exc}")
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
        _write_state(state, state_path)
        print(f"Persisted {item.get('source_file')}:{item.get('source_row_number')}: {persisted.title}")

    return PendingPersistSummary(confirmed_count=confirmed_count, failed_count=failed_count)


def _to_confirmed_input(candidate: ViewingHistoryCandidate, item: dict[str, Any]) -> ConfirmedViewingHistoryInput:
    subject_id = item.get("candidate_subject_id")
    if not subject_id:
        raise ValueError("review item has no candidate_subject_id")
    return ConfirmedViewingHistoryInput(
        source_raw_id=candidate.source_raw_id,
        source_file=candidate.source_file,
        source_row_number=candidate.source_row_number,
        douban_subject_id=subject_id,
        watched_date=candidate.watched_date,
        user_rating=candidate.user_rating,
        source_row_hash=candidate.source_row_hash,
        quality=candidate.quality,
        comment=candidate.comment,
    )


def _print_review_item(item: dict[str, Any], candidate: ViewingHistoryCandidate) -> None:
    print()
    print(f"{candidate.source_file}:{candidate.source_row_number}")
    print(f"Excel : {candidate.title} ({candidate.release_year or '-'})")
    if candidate.director:
        print(f"Excel director: {candidate.director}")
    print(f"Douban: {item.get('candidate_title') or '-'} ({item.get('candidate_year') or '-'})")
    if item.get("candidate_director"):
        print(f"Douban director: {item.get('candidate_director')}")
    print(f"Subject: https://movie.douban.com/subject/{item.get('candidate_subject_id')}/")
    print(f"Reason : {', '.join(item.get('match_reasons') or [])}")
    print("Persist title: Douban detail title")


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
def _load_state(state_path: str | Path) -> dict[str, Any]:
    path = Path(state_path)
    if not path.exists():
        raise FileNotFoundError(f"review state not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(state: dict[str, Any], state_path: str | Path) -> None:
    path = Path(state_path)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Manually review Douban needs_review matches and persist accepted rows.")
    parser.add_argument("excel_path", help="Path to MOVIES.xlsx or another viewing-history workbook.")
    parser.add_argument("--resume-state-path", default="data/cache/import-auto-match-progress.json")
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN. Defaults to MOVIES_POSTGRES_DSN or .env.")
    parser.add_argument("--config-path", default=".env", help="Local config file path. Defaults to .env.")
    parser.add_argument("--chrome-binary-path", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--detail-adapter", choices=("http", "selenium"), default="selenium")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    try:
        dsn = resolve_postgres_dsn(args.dsn, args.config_path)
    except ValueError as exc:
        parser.error(str(exc))

    repository = PostgresViewingHistoryRepository(dsn)
    repository.initialize_schema()
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
