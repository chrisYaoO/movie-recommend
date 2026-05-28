from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import sys
from typing import Callable, Protocol

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail
from backend.app.services.metadata_service import (
    DEFAULT_CHROME_BINARY_PATH,
    DoubanHttpDetailAdapter,
    DoubanSeleniumDetailAdapter,
)
from jobs.candidate_pool import DOUBAN_RECOMMENDATION_SOURCE, parse_recommended_subject_ids
from jobs.import_auto_matched_history import resolve_postgres_dsn

StatusWriter = Callable[[str], None]


class DoubanPageDetailAdapter(Protocol):
    def fetch(self, subject_id: str) -> DoubanMovieDetail: ...

    @property
    def last_page_source(self) -> str | None: ...


@dataclass(frozen=True)
class HistoryMovieRebuildSummary:
    pending_subject_count: int
    attempted_count: int
    fetched_count: int
    existing_count: int
    backfilled_history_count: int
    recommendation_discovered_count: int
    recommendation_inserted_count: int
    failed_count: int


def rebuild_movies_from_viewing_history(
    repository: ViewingHistoryRepository,
    detail_adapter: DoubanPageDetailAdapter,
    limit: int | None = None,
    dry_run: bool = False,
    status_writer: StatusWriter | None = None,
) -> HistoryMovieRebuildSummary:
    total_pending_subjects = len(repository.find_history_subject_ids_missing_movies())
    subject_ids = repository.find_history_subject_ids_missing_movies(limit=limit)
    _write_status(
        status_writer,
        f"[history-movies] pending_subjects={total_pending_subjects}, selected={len(subject_ids)}, limit={limit}, dry_run={dry_run}",
    )

    if dry_run:
        for subject_id in subject_ids:
            _write_status(status_writer, f"[history-movies] would_fetch subject={subject_id}")
        return HistoryMovieRebuildSummary(
            pending_subject_count=total_pending_subjects,
            attempted_count=0,
            fetched_count=0,
            existing_count=0,
            backfilled_history_count=0,
            recommendation_discovered_count=0,
            recommendation_inserted_count=0,
            failed_count=0,
        )

    attempted_count = 0
    fetched_count = 0
    existing_count = 0
    backfilled_history_count = 0
    recommendation_discovered_count = 0
    recommendation_inserted_count = 0
    failed_count = 0

    for subject_id in subject_ids:
        attempted_count += 1
        _write_status(
            status_writer,
            f"[history-movies] {attempted_count}/{len(subject_ids)} subject={subject_id}",
        )
        try:
            existing = repository.find_movie_by_subject_id(subject_id)
            if existing is not None:
                movie = existing
                existing_count += 1
                _write_status(status_writer, f"[history-movies] existing title={movie.title}")
            else:
                detail = detail_adapter.fetch(subject_id)
                movie = repository.upsert_movie_detail(detail)
                fetched_count += 1
                _write_status(status_writer, f"[history-movies] fetched title={movie.title}")

                page_source = detail_adapter.last_page_source
                if page_source:
                    recommended_ids = parse_recommended_subject_ids(page_source, subject_id)
                    recommendation_discovered_count += len(recommended_ids)
                    inserted_this_movie = 0
                    for recommended_id in recommended_ids:
                        if repository.upsert_candidate_subject(
                            recommended_id,
                            source_type=DOUBAN_RECOMMENDATION_SOURCE,
                            source_ref=f"recommended_from:{subject_id}",
                            source_subject_id=subject_id,
                            source_label=f"recommended from {movie.title}",
                        ):
                            recommendation_inserted_count += 1
                            inserted_this_movie += 1
                    _write_status(
                        status_writer,
                        f"[history-movies] recommendations discovered={len(recommended_ids)}, inserted={inserted_this_movie}",
                    )

            backfilled = repository.backfill_viewing_history_movie_id(subject_id, movie.id)
            backfilled_history_count += backfilled
            _write_status(status_writer, f"[history-movies] backfilled_history={backfilled}")
        except Exception as exc:
            failed_count += 1
            _write_status(status_writer, f"[history-movies] failed subject={subject_id}, error={exc}")

    return HistoryMovieRebuildSummary(
        pending_subject_count=total_pending_subjects,
        attempted_count=attempted_count,
        fetched_count=fetched_count,
        existing_count=existing_count,
        backfilled_history_count=backfilled_history_count,
        recommendation_discovered_count=recommendation_discovered_count,
        recommendation_inserted_count=recommendation_inserted_count,
        failed_count=failed_count,
    )


def _write_status(status_writer: StatusWriter | None, message: str) -> None:
    if status_writer is not None:
        status_writer(message)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Rebuild movies from viewing_history Douban subject IDs.")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--config-path", default=".env")
    parser.add_argument("--detail-adapter", choices=("http", "selenium"), default="selenium")
    parser.add_argument("--chrome-binary-path", default=DEFAULT_CHROME_BINARY_PATH)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repository = PostgresViewingHistoryRepository(resolve_postgres_dsn(args.dsn, args.config_path))
    repository.initialize_schema()
    detail_adapter = None
    try:
        if args.dry_run:
            detail_adapter = _DryRunDetailAdapter()
        elif args.detail_adapter == "http":
            detail_adapter = DoubanHttpDetailAdapter(
                timeout_seconds=args.timeout_seconds,
                delay_seconds=args.delay_seconds,
            )
        else:
            detail_adapter = DoubanSeleniumDetailAdapter(
                timeout_seconds=args.timeout_seconds,
                delay_seconds=args.delay_seconds,
                chrome_binary_path=args.chrome_binary_path,
            )

        result = rebuild_movies_from_viewing_history(
            repository=repository,
            detail_adapter=detail_adapter,
            limit=args.limit,
            dry_run=args.dry_run,
            status_writer=lambda message: print(message, file=sys.stderr, flush=True),
        )
    finally:
        if detail_adapter is not None and hasattr(detail_adapter, "close"):
            detail_adapter.close()
        repository.close()

    print(json.dumps({"summary": asdict(result)}, ensure_ascii=False, indent=2))


class _DryRunDetailAdapter:
    last_page_source: str | None = None

    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        raise RuntimeError("dry-run adapter should not fetch")


if __name__ == "__main__":
    main()
