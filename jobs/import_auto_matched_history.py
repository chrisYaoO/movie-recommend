from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import DoubanMatchStatus
from backend.app.services.history_persistence_service import (
    PersistConfirmedHistoryRunResult,
    persist_confirmed_viewing_history,
)
from backend.app.services.import_service import (
    InMemoryViewingHistoryRawRepository,
    ViewingHistoryImportService,
)
from backend.app.services.matching_service import (
    CachedDoubanSearchAdapter,
    DoubanHttpSearchAdapter,
    DoubanSearchAdapter,
    FileDoubanSearchCache,
    MatchRunResult,
    build_auto_matched_viewing_history_inputs,
    build_douban_match_inputs,
    run_search_match_job,
)
from backend.app.services.metadata_service import DoubanDetailAdapter, DoubanSeleniumDetailAdapter
from backend.app.services.metadata_service import DoubanHttpDetailAdapter


@dataclass(frozen=True)
class AutoImportSummary:
    imported_count: int
    skipped_duplicate_count: int
    skipped_invalid_count: int
    mapped_candidate_count: int
    mapping_issue_count: int
    auto_matched_count: int
    needs_review_skipped_count: int
    no_match_skipped_count: int
    persisted_count: int
    existing_count: int
    fetched_count: int
    failed_count: int


@dataclass(frozen=True)
class AutoImportRunResult:
    summary: AutoImportSummary
    match_result: MatchRunResult
    persistence_result: PersistConfirmedHistoryRunResult


@dataclass(frozen=True)
class ResumableAutoMatchSummary:
    metadata_candidate_count: int
    already_completed_count: int
    attempted_count: int
    auto_matched_count: int
    needs_review_count: int
    no_match_count: int
    persisted_count: int
    failed_count: int
    next_index: int | None


@dataclass(frozen=True)
class ResumableAutoMatchRunResult:
    summary: ResumableAutoMatchSummary
    state_path: str


def import_auto_matched_history(
    excel_path: str | Path,
    search_adapter: DoubanSearchAdapter,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
    subject_id_only: bool = False,
) -> AutoImportRunResult:
    import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
    import_result = import_service.import_excel(excel_path)
    mapping = import_service.to_viewing_history_candidates()
    candidates = (
        [candidate for candidate in mapping.candidates if candidate.douban_subject_id]
        if subject_id_only
        else mapping.candidates
    )
    match_queue = build_douban_match_inputs(candidates)
    match_result = run_search_match_job(match_queue.inputs, search_adapter)
    auto_confirmed_inputs = build_auto_matched_viewing_history_inputs(
        candidates,
        match_result.candidates,
    )
    persistence_result = persist_confirmed_viewing_history(
        auto_confirmed_inputs,
        detail_adapter,
        repository,
    )

    return AutoImportRunResult(
        summary=AutoImportSummary(
            imported_count=import_result.imported_count,
            skipped_duplicate_count=import_result.skipped_duplicate_count,
            skipped_invalid_count=import_result.skipped_invalid_count,
            mapped_candidate_count=len(candidates),
            mapping_issue_count=len(mapping.issues),
            auto_matched_count=sum(
                1 for candidate in match_result.candidates if candidate.status == DoubanMatchStatus.AUTO_MATCHED
            ),
            needs_review_skipped_count=sum(
                1 for candidate in match_result.candidates if candidate.status == DoubanMatchStatus.NEEDS_REVIEW
            ),
            no_match_skipped_count=sum(
                1 for candidate in match_result.candidates if candidate.status == DoubanMatchStatus.NO_MATCH
            ),
            persisted_count=persistence_result.persisted_count,
            existing_count=persistence_result.existing_count,
            fetched_count=persistence_result.fetched_count,
            failed_count=persistence_result.failed_count,
        ),
        match_result=match_result,
        persistence_result=persistence_result,
    )


def import_metadata_auto_matches_resumable(
    excel_path: str | Path,
    search_adapter: DoubanSearchAdapter,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
    state_path: str | Path,
    limit: int | None = None,
) -> ResumableAutoMatchRunResult:
    import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
    import_service.import_excel(excel_path)
    mapping = import_service.to_viewing_history_candidates()
    candidates = [candidate for candidate in mapping.candidates if not candidate.douban_subject_id]
    state = _load_resume_state(state_path)
    completed_hashes = _completed_hashes(state)

    attempted_count = 0
    auto_matched_count = 0
    needs_review_count = 0
    no_match_count = 0
    persisted_count = 0
    failed_count = 0
    next_index: int | None = None

    for index, candidate in enumerate(candidates):
        if candidate.source_row_hash in completed_hashes:
            continue
        if limit is not None and attempted_count >= limit:
            next_index = index
            break

        attempted_count += 1
        match_input = build_douban_match_inputs([candidate]).inputs[0]
        failure_recorded = False
        try:
            match = run_search_match_job([match_input], search_adapter).candidates[0]
            entry: dict[str, Any] = {
                "source_row_hash": candidate.source_row_hash,
                "source_raw_id": candidate.source_raw_id,
                "source_file": candidate.source_file,
                "source_row_number": candidate.source_row_number,
                "title": candidate.title,
                "release_year": candidate.release_year,
                "match_status": match.status.value,
                "match_score": match.match_score,
                "match_reasons": list(match.match_reasons),
                "candidate_subject_id": match.candidate_subject_id,
                "candidate_title": match.candidate_title,
                "candidate_year": match.candidate_year,
                "candidate_director": match.candidate_director,
            }

            if match.status == DoubanMatchStatus.AUTO_MATCHED:
                persistence = persist_confirmed_viewing_history(
                    build_auto_matched_viewing_history_inputs([candidate], [match]),
                    detail_adapter,
                    repository,
                )
                item = persistence.items[0]
                if item.status == "failed":
                    entry.update(
                        {
                            "status": "failed",
                            "error": item.error,
                        }
                    )
                    _record_resume_entry(state, entry, state_path)
                    failure_recorded = True
                    failed_count += 1
                    raise RuntimeError(item.error or "auto-matched persistence failed")

                entry.update(
                    {
                        "status": "auto_matched_persisted",
                        "persistence_status": item.status,
                        "movie_id": item.movie_id,
                        "viewing_history_id": item.viewing_history_id,
                        "persisted_title": item.title,
                    }
                )
                auto_matched_count += 1
                persisted_count += 1
            elif match.status == DoubanMatchStatus.NEEDS_REVIEW:
                entry["status"] = "needs_review"
                needs_review_count += 1
            else:
                entry["status"] = "no_match"
                no_match_count += 1

            _record_resume_entry(state, entry, state_path)
            completed_hashes.add(candidate.source_row_hash)
        except Exception as exc:
            if not failure_recorded:
                _record_resume_entry(
                    state,
                    {
                        "source_row_hash": candidate.source_row_hash,
                        "source_raw_id": candidate.source_raw_id,
                        "source_file": candidate.source_file,
                        "source_row_number": candidate.source_row_number,
                        "title": candidate.title,
                        "release_year": candidate.release_year,
                        "status": "failed",
                        "error": str(exc),
                    },
                    state_path,
                )
                failed_count += 1
            raise

    if next_index is None:
        next_index = _next_unfinished_index(candidates, _completed_hashes(state))

    return ResumableAutoMatchRunResult(
        summary=ResumableAutoMatchSummary(
            metadata_candidate_count=len(candidates),
            already_completed_count=len(completed_hashes),
            attempted_count=attempted_count,
            auto_matched_count=auto_matched_count,
            needs_review_count=needs_review_count,
            no_match_count=no_match_count,
            persisted_count=persisted_count,
            failed_count=failed_count,
            next_index=next_index,
        ),
        state_path=str(state_path),
    )


def _load_resume_state(state_path: str | Path) -> dict[str, Any]:
    path = Path(state_path)
    if not path.exists():
        return {"items": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _record_resume_entry(state: dict[str, Any], entry: dict[str, Any], state_path: str | Path) -> None:
    state.setdefault("items", []).append(entry)
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _completed_hashes(state: dict[str, Any]) -> set[str]:
    completed_statuses = {"auto_matched_persisted", "needs_review", "no_match"}
    return {
        item["source_row_hash"]
        for item in state.get("items", [])
        if item.get("status") in completed_statuses and item.get("source_row_hash")
    }


def _next_unfinished_index(candidates, completed_hashes: set[str]) -> int | None:
    for index, candidate in enumerate(candidates):
        if candidate.source_row_hash not in completed_hashes:
            return index
    return None


def resolve_postgres_dsn(dsn_arg: str | None, config_path: str | Path = ".env") -> str:
    env_dsn = os.getenv("MOVIES_POSTGRES_DSN")
    config_dsn = _load_config_value(config_path, "MOVIES_POSTGRES_DSN")
    env_literals = {"$env:MOVIES_POSTGRES_DSN", "%MOVIES_POSTGRES_DSN%"}
    if dsn_arg and dsn_arg not in env_literals:
        return dsn_arg
    if env_dsn:
        return env_dsn
    if config_dsn:
        return config_dsn
    if dsn_arg in env_literals:
        raise ValueError(
            f"{dsn_arg} was passed literally; set MOVIES_POSTGRES_DSN, add it to {config_path}, or pass the actual DSN."
        )
    raise ValueError(f"PostgreSQL DSN is required. Pass --dsn, set MOVIES_POSTGRES_DSN, or add it to {config_path}.")


def _load_config_value(config_path: str | Path, key: str) -> str | None:
    path = Path(config_path)
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        return value.strip().strip('"').strip("'") or None
    return None


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Import auto-matched Excel viewing history into PostgreSQL.")
    parser.add_argument("excel_path", help="Path to MOVIES.xlsx or another viewing-history workbook.")
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN. Defaults to MOVIES_POSTGRES_DSN or .env.")
    parser.add_argument("--config-path", default=".env", help="Local config file path. Defaults to .env.")
    parser.add_argument("--chrome-binary-path", default=None)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--detail-adapter", choices=("http", "selenium"), default="selenium")
    parser.add_argument("--search-cache-dir", default="data/cache/douban-search")
    parser.add_argument("--resume-state-path", default="data/cache/import-auto-match-progress.json")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--subject-id-only",
        action="store_true",
        help="Only import rows that already have a Douban subject id in Excel; skip metadata search rows.",
    )
    parser.add_argument(
        "--metadata-search-resume",
        action="store_true",
        help="Search metadata-only rows one by one, persist AUTO_MATCHED rows immediately, and checkpoint every row.",
    )
    args = parser.parse_args()

    try:
        dsn = resolve_postgres_dsn(args.dsn, args.config_path)
    except ValueError as exc:
        parser.error(str(exc))

    repository = PostgresViewingHistoryRepository(dsn)
    repository.initialize_schema()
    search_adapter = CachedDoubanSearchAdapter(
        DoubanHttpSearchAdapter(timeout_seconds=args.timeout_seconds, delay_seconds=args.delay_seconds),
        FileDoubanSearchCache(args.search_cache_dir),
    )
    if args.detail_adapter == "http":
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

    try:
        if args.metadata_search_resume:
            result = import_metadata_auto_matches_resumable(
                args.excel_path,
                search_adapter,
                detail_adapter,
                repository,
                state_path=args.resume_state_path,
                limit=args.limit,
            )
        else:
            result = import_auto_matched_history(
                args.excel_path,
                search_adapter,
                detail_adapter,
                repository,
                subject_id_only=args.subject_id_only,
            )
    finally:
        if hasattr(detail_adapter, "close"):
            detail_adapter.close()
        repository.close()

    print(
        json.dumps(
            {
                "summary": asdict(result.summary),
                **(
                    {"state_path": result.state_path}
                    if isinstance(result, ResumableAutoMatchRunResult)
                    else {"persistence_items": [asdict(item) for item in result.persistence_result.items]}
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
