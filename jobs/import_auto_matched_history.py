from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable
from uuid import uuid4

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import DoubanMatchStatus
from backend.app.services.history_persistence_service import (
    PersistConfirmedHistoryRunResult,
    persist_confirmed_viewing_history,
)
from backend.app.services.import_service import (
    InMemoryViewingHistoryRawRepository,
    read_viewing_history_excel,
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
from backend.app.services.metadata_service import (
    DEFAULT_CHROME_BINARY_PATH,
    DoubanDetailAdapter,
    DoubanHttpDetailAdapter,
    DoubanSeleniumDetailAdapter,
)

StatusWriter = Callable[[str], None]


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
    state_path: str | None = None


@dataclass(frozen=True)
class ResumableAutoMatchSummary:
    metadata_candidate_count: int
    already_completed_count: int
    attempted_count: int
    auto_matched_count: int
    needs_review_count: int
    no_match_count: int
    persisted_count: int
    existing_count: int
    fetched_count: int
    failed_count: int
    next_index: int | None


@dataclass(frozen=True)
class ResumableAutoMatchRunResult:
    summary: ResumableAutoMatchSummary
    state_path: str


@dataclass(frozen=True)
class MetadataCacheDiagnostics:
    metadata_count: int
    cache_hit_count: int | None
    cache_miss_count: int | None
    first_miss: Any | None


@dataclass(frozen=True)
class RetryNoYearMatchSummary:
    candidate_count: int
    attempted_count: int
    updated_to_needs_review_count: int
    kept_no_match_count: int
    skipped_count: int


@dataclass(frozen=True)
class CompletedResumeItems:
    checksums: set[str]
    source_rows: set[tuple[str, int]]

    def contains(self, candidate) -> bool:
        if candidate.source_row_checksum in self.checksums:
            return True
        return (candidate.source_sheet_name, candidate.source_row_number) in self.source_rows

    def add(self, candidate) -> None:
        if candidate.source_row_checksum:
            self.checksums.add(candidate.source_row_checksum)
        self.source_rows.add((candidate.source_sheet_name, candidate.source_row_number))

    def __len__(self) -> int:
        return len(self.checksums)


def import_auto_matched_history(
    excel_path: str | Path,
    search_adapter: DoubanSearchAdapter,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
    state_path: str | Path = "data/cache/import-auto-match-progress.json",
    limit: int | None = None,
    status_writer: StatusWriter | None = None,
) -> AutoImportRunResult:
    excel_path = Path(excel_path)
    return import_auto_matched_rows(
        source_sheet_name=excel_path.name,
        rows=read_viewing_history_excel(excel_path),
        search_adapter=search_adapter,
        detail_adapter=detail_adapter,
        repository=repository,
        state_path=state_path,
        limit=limit,
        status_writer=status_writer,
    )


def import_auto_matched_rows(
    source_sheet_name: str,
    rows: list[dict[str, Any]],
    search_adapter: DoubanSearchAdapter,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
    state_path: str | Path = "data/cache/import-auto-match-progress.json",
    limit: int | None = None,
    status_writer: StatusWriter | None = None,
) -> AutoImportRunResult:
    import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
    import_result = import_service.import_rows(source_sheet_name, rows)
    mapping = import_service.to_viewing_history_candidates()
    candidates = mapping.candidates
    subject_id_candidates = [candidate for candidate in candidates if candidate.douban_subject_id]
    metadata_candidates = [candidate for candidate in candidates if not candidate.douban_subject_id]

    _write_status(
        status_writer,
        "[import] "
        f"imported={import_result.imported_count}, "
        f"duplicates={import_result.skipped_duplicate_count}, "
        f"invalid={import_result.skipped_invalid_count}, "
        f"mapped={len(candidates)}, "
        f"subject_id={len(subject_id_candidates)}, "
        f"metadata={len(metadata_candidates)}, "
        f"mapping_issues={len(mapping.issues)}",
    )

    subject_id_match_result = run_search_match_job(
        build_douban_match_inputs(subject_id_candidates).inputs,
        search_adapter,
    )
    _write_status(status_writer, f"[import] persisting subject-id rows: {len(subject_id_candidates)}")
    subject_id_persistence_result = persist_confirmed_viewing_history(
        build_auto_matched_viewing_history_inputs(subject_id_candidates, subject_id_match_result.candidates),
        detail_adapter,
        repository,
    )
    _write_status(
        status_writer,
        "[import] "
        f"subject-id persisted={subject_id_persistence_result.persisted_count}, "
        f"existing={subject_id_persistence_result.existing_count}, "
        f"fetched={subject_id_persistence_result.fetched_count}, "
        f"failed={subject_id_persistence_result.failed_count}",
    )

    metadata_result = import_metadata_auto_matches_from_rows_resumable(
        source_sheet_name=source_sheet_name,
        rows=rows,
        search_adapter=search_adapter,
        detail_adapter=detail_adapter,
        repository=repository,
        state_path=state_path,
        limit=limit,
        status_writer=status_writer,
    )

    return AutoImportRunResult(
        summary=AutoImportSummary(
            imported_count=import_result.imported_count,
            skipped_duplicate_count=import_result.skipped_duplicate_count,
            skipped_invalid_count=import_result.skipped_invalid_count,
            mapped_candidate_count=len(candidates),
            mapping_issue_count=len(mapping.issues),
            auto_matched_count=sum(
                1 for candidate in subject_id_match_result.candidates if candidate.status == DoubanMatchStatus.AUTO_MATCHED
            )
            + metadata_result.summary.auto_matched_count,
            needs_review_skipped_count=metadata_result.summary.needs_review_count,
            no_match_skipped_count=metadata_result.summary.no_match_count,
            persisted_count=subject_id_persistence_result.persisted_count + metadata_result.summary.persisted_count,
            existing_count=subject_id_persistence_result.existing_count + metadata_result.summary.existing_count,
            fetched_count=subject_id_persistence_result.fetched_count + metadata_result.summary.fetched_count,
            failed_count=subject_id_persistence_result.failed_count + metadata_result.summary.failed_count,
        ),
        match_result=MatchRunResult(candidates=subject_id_match_result.candidates),
        persistence_result=subject_id_persistence_result,
        state_path=metadata_result.state_path,
    )


def import_metadata_auto_matches_resumable(
    excel_path: str | Path,
    search_adapter: DoubanSearchAdapter,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
    state_path: str | Path,
    limit: int | None = None,
    status_writer: StatusWriter | None = None,
) -> ResumableAutoMatchRunResult:
    excel_path = Path(excel_path)
    return import_metadata_auto_matches_from_rows_resumable(
        source_sheet_name=excel_path.name,
        rows=read_viewing_history_excel(excel_path),
        search_adapter=search_adapter,
        detail_adapter=detail_adapter,
        repository=repository,
        state_path=state_path,
        limit=limit,
        status_writer=status_writer,
    )


def import_metadata_auto_matches_from_rows_resumable(
    source_sheet_name: str,
    rows: list[dict[str, Any]],
    search_adapter: DoubanSearchAdapter,
    detail_adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
    state_path: str | Path,
    limit: int | None = None,
    status_writer: StatusWriter | None = None,
) -> ResumableAutoMatchRunResult:
    import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
    import_service.import_rows(source_sheet_name, rows)
    mapping = import_service.to_viewing_history_candidates()
    candidates = [candidate for candidate in mapping.candidates if not candidate.douban_subject_id]
    metadata_inputs = build_douban_match_inputs(candidates).inputs
    state = _load_resume_state(state_path)
    completed_items = _completed_resume_items(state)
    remaining_count = sum(1 for candidate in candidates if not completed_items.contains(candidate))
    completed_candidate_count = len(candidates) - remaining_count
    diagnostics = _report_metadata_cache_diagnostics(status_writer, metadata_inputs, search_adapter)
    _write_status(
        status_writer,
        f"[resume] metadata={len(candidates)}, completed={completed_candidate_count}, remaining={remaining_count}, limit={limit}",
    )

    attempted_count = 0
    auto_matched_count = 0
    needs_review_count = 0
    no_match_count = 0
    persisted_count = 0
    existing_count = 0
    fetched_count = 0
    failed_count = 0
    next_index: int | None = None

    for index, candidate in enumerate(candidates):
        if completed_items.contains(candidate):
            continue
        if limit is not None and attempted_count >= limit:
            next_index = index
            break

        attempted_count += 1
        match_input = build_douban_match_inputs([candidate]).inputs[0]
        failure_recorded = False
        try:
            _write_status(
                status_writer,
                "[match] "
                f"metadata {attempted_count}/{remaining_count}: "
                f"row {match_input.source_row_number}, title = {match_input.title}, "
                f"year = {match_input.release_year}, cache = {_cache_status(search_adapter, match_input)}",
            )
            match = run_search_match_job([match_input], search_adapter).candidates[0]
            _write_status(
                status_writer,
                f"[match] status = {match.status.value}, score = {match.match_score}",
            )
            entry: dict[str, Any] = {
                "source_row_checksum": candidate.source_row_checksum,
                "source_raw_id": candidate.source_raw_id,
                "source_sheet_name": candidate.source_sheet_name,
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
                if item.status == "existing":
                    existing_count += 1
                elif item.status == "fetched":
                    fetched_count += 1
            elif match.status == DoubanMatchStatus.NEEDS_REVIEW:
                entry["status"] = "needs_review"
                needs_review_count += 1
            else:
                entry["status"] = "no_match"
                no_match_count += 1

            _record_resume_entry(state, entry, state_path)
            completed_items.add(candidate)
        except Exception as exc:
            _write_status(
                status_writer,
                "[error] "
                f"metadata row failed: row {candidate.source_row_number}, "
                f"title = {candidate.title}, year = {candidate.release_year}, error = {exc}",
            )
            if not failure_recorded:
                _record_resume_entry(
                    state,
                    {
                        "source_row_checksum": candidate.source_row_checksum,
                        "source_raw_id": candidate.source_raw_id,
                        "source_sheet_name": candidate.source_sheet_name,
                        "source_row_number": candidate.source_row_number,
                        "title": candidate.title,
                        "release_year": candidate.release_year,
                        "status": "failed",
                        "error": str(exc),
                    },
                    state_path,
                )
                failed_count += 1
            _write_metadata_run_summary(
                status_writer,
                attempted_count=attempted_count,
                auto_matched_count=auto_matched_count,
                needs_review_count=needs_review_count,
                no_match_count=no_match_count,
                persisted_count=persisted_count,
                failed_count=failed_count,
            )
            raise

    if next_index is None:
        next_index = _next_unfinished_index(candidates, _completed_resume_items(state))

    _write_metadata_run_summary(
        status_writer,
        attempted_count=attempted_count,
        auto_matched_count=auto_matched_count,
        needs_review_count=needs_review_count,
        no_match_count=no_match_count,
        persisted_count=persisted_count,
        failed_count=failed_count,
    )

    return ResumableAutoMatchRunResult(
        summary=ResumableAutoMatchSummary(
            metadata_candidate_count=len(candidates),
            already_completed_count=completed_candidate_count,
            attempted_count=attempted_count,
            auto_matched_count=auto_matched_count,
            needs_review_count=needs_review_count,
            no_match_count=no_match_count,
            persisted_count=persisted_count,
            existing_count=existing_count,
            fetched_count=fetched_count,
            failed_count=failed_count,
            next_index=next_index,
        ),
        state_path=str(state_path),
    )


def retry_no_year_match_no_matches(
    excel_path: str | Path,
    search_adapter: DoubanSearchAdapter,
    state_path: str | Path,
    limit: int | None = None,
    status_writer: StatusWriter | None = None,
) -> RetryNoYearMatchSummary:
    import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
    import_service.import_excel(excel_path)
    mapping = import_service.to_viewing_history_candidates()
    candidates_by_checksum = {
        candidate.source_row_checksum: candidate
        for candidate in mapping.candidates
        if candidate.source_row_checksum is not None
    }
    candidates_by_source_row = {
        (candidate.source_sheet_name, candidate.source_row_number): candidate
        for candidate in mapping.candidates
    }
    candidates_by_title_year: dict[tuple[str, int | None], Any] = {}
    duplicate_title_year_keys: set[tuple[str, int | None]] = set()
    for candidate in mapping.candidates:
        key = _candidate_title_year_key(candidate.title, candidate.release_year)
        if key in candidates_by_title_year:
            duplicate_title_year_keys.add(key)
        else:
            candidates_by_title_year[key] = candidate
    for key in duplicate_title_year_keys:
        candidates_by_title_year.pop(key, None)
    state = _load_resume_state(state_path)
    retry_items = [
        item
        for item in state.get("items", [])
        if item.get("status") == "no_match"
        and "douban_search_no_year_match" in (item.get("match_reasons") or [])
        and _progress_row_checksum(item)
    ]

    attempted_count = 0
    updated_to_needs_review_count = 0
    kept_no_match_count = 0
    skipped_count = 0
    for item in retry_items:
        if limit is not None and attempted_count >= limit:
            break

        candidate = _resolve_candidate_for_resume_item(
            item,
            candidates_by_checksum,
            candidates_by_source_row,
            candidates_by_title_year,
        )
        if candidate is None:
            skipped_count += 1
            continue

        attempted_count += 1
        match_input = build_douban_match_inputs([candidate]).inputs[0]
        _write_status(
            status_writer,
            "[retry-no-year-match] "
            f"{attempted_count}/{len(retry_items)}: row {match_input.source_row_number}, "
            f"title = {match_input.title}, year = {match_input.release_year}, "
            f"cache = {_cache_status(search_adapter, match_input)}",
        )
        match = run_search_match_job([match_input], search_adapter).candidates[0]
        item.update(
            {
                "source_row_checksum": candidate.source_row_checksum,
                "source_raw_id": candidate.source_raw_id,
                "source_sheet_name": candidate.source_sheet_name,
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
        )
        if match.status == DoubanMatchStatus.NO_MATCH:
            item["status"] = "no_match"
            kept_no_match_count += 1
        else:
            item["status"] = "needs_review"
            item.pop("review_status", None)
            item.pop("review_error", None)
            updated_to_needs_review_count += 1
        _write_resume_state(state, state_path)

    _write_status(
        status_writer,
        "[retry-no-year-match] "
        f"attempted={attempted_count}, "
        f"updated_to_needs_review={updated_to_needs_review_count}, "
        f"kept_no_match={kept_no_match_count}, "
        f"skipped={skipped_count}",
    )
    return RetryNoYearMatchSummary(
        candidate_count=len(retry_items),
        attempted_count=attempted_count,
        updated_to_needs_review_count=updated_to_needs_review_count,
        kept_no_match_count=kept_no_match_count,
        skipped_count=skipped_count,
    )


def _resolve_candidate_for_resume_item(
    item: dict[str, Any],
    candidates_by_checksum: dict[str, Any],
    candidates_by_source_row: dict[tuple[str, int], Any],
    candidates_by_title_year: dict[tuple[str, int | None], Any],
):
    source_row_checksum = _progress_row_checksum(item)
    if source_row_checksum:
        candidate = candidates_by_checksum.get(source_row_checksum)
        if candidate is not None:
            return candidate

    source_sheet_name = _progress_source_sheet_name(item)
    source_row_number = item.get("source_row_number")
    if source_sheet_name and isinstance(source_row_number, int):
        candidate = candidates_by_source_row.get((source_sheet_name, source_row_number))
        if candidate is not None:
            return candidate

    return candidates_by_title_year.get(_candidate_title_year_key(item.get("title"), item.get("release_year")))


def _candidate_title_year_key(title: Any, release_year: Any) -> tuple[str, int | None]:
    normalized_title = str(title or "").strip().casefold()
    year = release_year if isinstance(release_year, int) else None
    return (normalized_title, year)


def _report_metadata_cache_diagnostics(
    status_writer: StatusWriter | None,
    metadata_inputs: list,
    search_adapter: DoubanSearchAdapter,
) -> MetadataCacheDiagnostics:
    diagnostics = _metadata_cache_diagnostics(metadata_inputs, search_adapter)
    _write_metadata_cache_diagnostics(status_writer, diagnostics)
    return diagnostics


def _metadata_cache_diagnostics(metadata_inputs: list, search_adapter: DoubanSearchAdapter) -> MetadataCacheDiagnostics:
    cache = getattr(search_adapter, "cache", None)
    if cache is None:
        return MetadataCacheDiagnostics(
            metadata_count=len(metadata_inputs),
            cache_hit_count=None,
            cache_miss_count=None,
            first_miss=None,
        )

    hit_count = 0
    miss_count = 0
    first_miss = None
    for match_input in metadata_inputs:
        if cache.get(match_input) is None:
            miss_count += 1
            if first_miss is None:
                first_miss = match_input
        else:
            hit_count += 1

    return MetadataCacheDiagnostics(
        metadata_count=len(metadata_inputs),
        cache_hit_count=hit_count,
        cache_miss_count=miss_count,
        first_miss=first_miss,
    )


def _write_metadata_cache_diagnostics(
    status_writer: StatusWriter | None,
    diagnostics: MetadataCacheDiagnostics,
) -> None:
    _write_status(status_writer, f"[metadata] rows without id: {diagnostics.metadata_count}")
    if diagnostics.cache_hit_count is None or diagnostics.cache_miss_count is None:
        _write_status(status_writer, "[metadata] cache hits: unknown")
        _write_status(status_writer, "[metadata] cache misses: unknown")
        return

    _write_status(status_writer, f"[metadata] cache hits: {diagnostics.cache_hit_count}")
    _write_status(status_writer, f"[metadata] cache misses: {diagnostics.cache_miss_count}")


def _write_metadata_run_summary(
    status_writer: StatusWriter | None,
    attempted_count: int,
    auto_matched_count: int,
    needs_review_count: int,
    no_match_count: int,
    persisted_count: int,
    failed_count: int,
) -> None:
    _write_status(
        status_writer,
        "[resume] "
        f"attempted this run: {attempted_count}, "
        f"auto_matched={auto_matched_count}, "
        f"needs_review={needs_review_count}, "
        f"no_match={no_match_count}, "
        f"persisted={persisted_count}, "
        f"failed={failed_count}",
    )


def _cache_status(search_adapter: DoubanSearchAdapter, match_input) -> str:
    cache = getattr(search_adapter, "cache", None)
    if cache is None:
        return "unknown"
    return "hit" if cache.get(match_input) is not None else "miss"


def _write_status(status_writer: StatusWriter | None, message: str) -> None:
    if status_writer is not None:
        status_writer(message)


def _load_resume_state(state_path: str | Path) -> dict[str, Any]:
    path = Path(state_path)
    if not path.exists():
        return {"items": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _record_resume_entry(state: dict[str, Any], entry: dict[str, Any], state_path: str | Path) -> None:
    state.setdefault("items", []).append(entry)
    _write_resume_state(state, state_path)


def _write_resume_state(state: dict[str, Any], state_path: str | Path) -> None:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        _replace_with_retries(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _replace_with_retries(temp_path: Path, target_path: Path, attempts: int = 5, delay_seconds: float = 0.2) -> None:
    for attempt in range(attempts):
        try:
            temp_path.replace(target_path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay_seconds)


def _completed_checksums(state: dict[str, Any]) -> set[str]:
    return _completed_resume_items(state).checksums


def _completed_hashes(state: dict[str, Any]) -> set[str]:
    return _completed_checksums(state)


def _completed_resume_items(state: dict[str, Any]) -> CompletedResumeItems:
    completed_statuses = {
        "auto_matched_persisted",
        "needs_review",
        "no_match",
        "review_confirmed_persisted",
        "review_rejected",
        "manual_id_persisted",
        "manual_id_rejected",
    }
    checksums: set[str] = set()
    source_rows: set[tuple[str, int]] = set()
    for item in state.get("items", []):
        if item.get("status") not in completed_statuses:
            continue
        source_row_checksum = _progress_row_checksum(item)
        if source_row_checksum:
            checksums.add(source_row_checksum)
        source_sheet_name = _progress_source_sheet_name(item)
        source_row_number = item.get("source_row_number")
        if source_sheet_name and isinstance(source_row_number, int):
            source_rows.add((source_sheet_name, source_row_number))
    return CompletedResumeItems(checksums=checksums, source_rows=source_rows)


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


def _next_unfinished_index(candidates, completed_items: CompletedResumeItems) -> int | None:
    for index, candidate in enumerate(candidates):
        if not completed_items.contains(candidate):
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
    parser.add_argument("--chrome-binary-path", default=DEFAULT_CHROME_BINARY_PATH)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--detail-adapter", choices=("http", "selenium"), default="selenium")
    parser.add_argument("--search-cache-dir", default="data/cache/douban-search")
    parser.add_argument("--resume-state-path", default="data/cache/import-auto-match-progress.json")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--retry-no-year-match-no-matches",
        action="store_true",
        help="Re-run state entries with status=no_match and reason=douban_search_no_year_match under current rules.",
    )
    args = parser.parse_args()

    search_adapter = CachedDoubanSearchAdapter(
        DoubanHttpSearchAdapter(timeout_seconds=args.timeout_seconds, delay_seconds=args.delay_seconds),
        FileDoubanSearchCache(args.search_cache_dir),
    )
    status_writer = lambda message: print(message, file=sys.stderr, flush=True)

    if args.retry_no_year_match_no_matches:
        result = retry_no_year_match_no_matches(
            args.excel_path,
            search_adapter,
            args.resume_state_path,
            limit=args.limit,
            status_writer=status_writer,
        )
        print(json.dumps({"summary": asdict(result), "state_path": args.resume_state_path}, ensure_ascii=False, indent=2))
        return

    try:
        dsn = resolve_postgres_dsn(args.dsn, args.config_path)
    except ValueError as exc:
        parser.error(str(exc))

    repository = PostgresViewingHistoryRepository(dsn)
    repository.initialize_schema()
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
        result = import_auto_matched_history(
            args.excel_path,
            search_adapter,
            detail_adapter,
            repository,
            state_path=args.resume_state_path,
            limit=args.limit,
            status_writer=status_writer,
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
                    if result.state_path is not None
                    else {"persistence_items": [asdict(item) for item in result.persistence_result.items]}
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()


