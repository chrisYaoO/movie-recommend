from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Callable, Iterable, Mapping

from backend.app.config import resolve_postgres_dsn
from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail
from backend.app.services.metadata_service import (
    DEFAULT_CHROME_BINARY_PATH,
    DoubanDetailAdapter,
    DoubanSeleniumDetailAdapter,
)


DEFAULT_CHECKPOINT_PATH = Path("data/cache/refresh-local-person-names-progress.json")
CHECKPOINT_VERSION = 1
_ASCII_LETTER = re.compile(r"[A-Za-z]")
_CJK_LETTER = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_MIDDLE_DOTS = ("·", "・", "•", ".", "┞")
_EAST_ASIAN_COUNTRIES = {
    "中国",
    "中国大陆",
    "中国香港",
    "中国台湾",
    "香港",
    "台湾",
    "日本",
    "韩国",
    "朝鲜",
}
StatusWriter = Callable[[str], None]


@dataclass(frozen=True)
class PersonNameIssue:
    role: str
    stored_name: str
    expected_local_names: tuple[str, ...]


@dataclass(frozen=True)
class MovieRefreshCandidate:
    subject_id: str
    title: str
    issues: tuple[PersonNameIssue, ...]


@dataclass(frozen=True)
class RefreshSummary:
    total_candidate_count: int
    selected_count: int
    skipped_completed_count: int
    attempted_count: int
    updated_count: int
    failed_count: int


def find_refresh_candidates(rows: Iterable[Mapping[str, object]]) -> list[MovieRefreshCandidate]:
    movie_rows = list(rows)
    local_names_by_foreign: dict[str, set[str]] = {}
    for row in movie_rows:
        for field in ("directors", "actors"):
            for name in _raw_person_names(row, field):
                split = _split_local_and_foreign(name)
                if split is None:
                    continue
                local_name, foreign_name = split
                local_names_by_foreign.setdefault(foreign_name.casefold(), set()).add(local_name)

    candidates = []
    for row in movie_rows:
        issues = []
        seen_issues = set()
        is_east_asian_movie = bool(set(_raw_countries(row)) & _EAST_ASIAN_COUNTRIES)
        for field, role in (("directors", "director"), ("actors", "actor")):
            for name in _raw_person_names(row, field):
                if _CJK_LETTER.search(name) or _ASCII_LETTER.search(name) is None:
                    continue
                expected = local_names_by_foreign.get(name.casefold())
                issue_key = (role, name.casefold())
                if (not expected and not is_east_asian_movie) or issue_key in seen_issues:
                    continue
                seen_issues.add(issue_key)
                issues.append(
                    PersonNameIssue(
                        role=role,
                        stored_name=name,
                        expected_local_names=tuple(sorted(expected or ())),
                    )
                )
        if issues:
            candidates.append(
                MovieRefreshCandidate(
                    subject_id=str(row["douban_subject_id"]),
                    title=str(row["title"]),
                    issues=tuple(issues),
                )
            )
    return candidates


def refresh_local_person_names(
    repository,
    detail_adapter: DoubanDetailAdapter,
    candidates: Iterable[MovieRefreshCandidate],
    checkpoint_path: str | Path = DEFAULT_CHECKPOINT_PATH,
    limit: int | None = None,
    dry_run: bool = False,
    status_writer: StatusWriter | None = None,
) -> RefreshSummary:
    candidate_list = list(candidates)
    checkpoint_file = Path(checkpoint_path)
    checkpoint = _load_checkpoint(checkpoint_file)
    completed = checkpoint["completed"]
    pending = [candidate for candidate in candidate_list if candidate.subject_id not in completed]
    skipped_completed_count = len(candidate_list) - len(pending)
    selected = pending[:limit] if limit is not None else pending

    _write_status(
        status_writer,
        f"[local-names] candidates={len(candidate_list)}, completed={skipped_completed_count}, "
        f"selected={len(selected)}, limit={limit}, dry_run={dry_run}",
    )
    if dry_run:
        for candidate in selected:
            issue_text = ", ".join(f"{issue.role}:{issue.stored_name}" for issue in candidate.issues)
            _write_status(
                status_writer,
                f"[local-names] would_fetch subject={candidate.subject_id}, title={candidate.title}, issues={issue_text}",
            )
        return RefreshSummary(
            total_candidate_count=len(candidate_list),
            selected_count=len(selected),
            skipped_completed_count=skipped_completed_count,
            attempted_count=0,
            updated_count=0,
            failed_count=0,
        )

    updated_count = 0
    failed_count = 0
    for index, candidate in enumerate(selected, start=1):
        _write_status(
            status_writer,
            f"[local-names] {index}/{len(selected)} subject={candidate.subject_id}, title={candidate.title}",
        )
        try:
            detail = detail_adapter.fetch(candidate.subject_id)
            _validate_refreshed_detail(candidate, detail)
            repository.upsert_movie_detail(detail)
            checkpoint["completed"][candidate.subject_id] = {
                "completed_at": _utc_now(),
                "title": detail.title,
                "directors": list(detail.directors),
                "actors": list(detail.actors),
            }
            checkpoint["failures"].pop(candidate.subject_id, None)
            updated_count += 1
            _write_status(
                status_writer,
                f"[local-names] updated subject={candidate.subject_id}, directors={list(detail.directors)}",
            )
        except Exception as exc:
            previous = checkpoint["failures"].get(candidate.subject_id) or {}
            checkpoint["failures"][candidate.subject_id] = {
                "attempts": int(previous.get("attempts") or 0) + 1,
                "last_failed_at": _utc_now(),
                "error": str(exc),
            }
            failed_count += 1
            _write_status(
                status_writer,
                f"[local-names] failed subject={candidate.subject_id}, error={exc}",
            )
        _save_checkpoint(checkpoint_file, checkpoint)

    return RefreshSummary(
        total_candidate_count=len(candidate_list),
        selected_count=len(selected),
        skipped_completed_count=skipped_completed_count,
        attempted_count=len(selected),
        updated_count=updated_count,
        failed_count=failed_count,
    )


def _validate_refreshed_detail(candidate: MovieRefreshCandidate, detail: DoubanMovieDetail) -> None:
    if detail.subject_id != candidate.subject_id:
        raise ValueError(f"fetched subject {detail.subject_id} does not match {candidate.subject_id}")
    required_roles = {issue.role for issue in candidate.issues}
    if "director" in required_roles and not detail.directors:
        raise ValueError("fresh JSON-LD did not contain director metadata")
    if "actor" in required_roles and not detail.actors:
        raise ValueError("fresh JSON-LD did not contain actor metadata")


def _split_local_and_foreign(name: str) -> tuple[str, str] | None:
    match = _ASCII_LETTER.search(name)
    if match is None:
        return None
    local_name = name[: match.start()].strip()
    foreign_name = name[match.start() :].strip()
    if (
        not local_name
        or not foreign_name
        or _CJK_LETTER.search(local_name) is None
        or any(marker in local_name for marker in _MIDDLE_DOTS)
    ):
        return None
    return local_name, foreign_name


def _raw_person_names(row: Mapping[str, object], field: str) -> list[str]:
    payload = _raw_payload(row)
    values = payload.get(field) or []
    return [str(value).strip() for value in values if value]


def _raw_countries(row: Mapping[str, object]) -> list[str]:
    payload = _raw_payload(row)
    values = payload.get("countries") or row.get("countries") or []
    return [str(value).strip() for value in values if value]


def _raw_payload(row: Mapping[str, object]) -> dict:
    payload = row.get("raw_douban_json") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return {}
    return payload


def _load_checkpoint(path: Path) -> dict:
    if not path.exists():
        return {"version": CHECKPOINT_VERSION, "completed": {}, "failures": {}}
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    if checkpoint.get("version") != CHECKPOINT_VERSION:
        raise ValueError(f"unsupported checkpoint version in {path}")
    checkpoint.setdefault("completed", {})
    checkpoint.setdefault("failures", {})
    return checkpoint


def _save_checkpoint(path: Path, checkpoint: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _load_movie_rows(repository: PostgresViewingHistoryRepository) -> list[dict]:
    return repository.connection.execute(
        """
        SELECT douban_subject_id, title, countries, raw_douban_json
        FROM movies
        ORDER BY douban_subject_id
        """
    ).fetchall()


def _write_status(status_writer: StatusWriter | None, message: str) -> None:
    if status_writer is not None:
        status_writer(message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Refresh likely-stale person names from current Douban JSON-LD; resumes from a JSON checkpoint."
    )
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--config-path", default=".env")
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT_PATH)
    parser.add_argument("--chrome-binary-path", default=DEFAULT_CHROME_BINARY_PATH)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repository = PostgresViewingHistoryRepository(resolve_postgres_dsn(args.dsn, args.config_path))
    detail_adapter = None
    try:
        candidates = find_refresh_candidates(_load_movie_rows(repository))
        if not args.dry_run:
            detail_adapter = DoubanSeleniumDetailAdapter(
                timeout_seconds=args.timeout_seconds,
                delay_seconds=args.delay_seconds,
                chrome_binary_path=args.chrome_binary_path,
                headless=not args.headed,
            )
        result = refresh_local_person_names(
            repository=repository,
            detail_adapter=detail_adapter or _DryRunDetailAdapter(),
            candidates=candidates,
            checkpoint_path=args.checkpoint,
            limit=args.limit,
            dry_run=args.dry_run,
            status_writer=lambda message: print(message, file=sys.stderr, flush=True),
        )
    finally:
        if detail_adapter is not None:
            detail_adapter.close()
        repository.close()

    print(
        json.dumps(
            {
                "checkpoint": str(args.checkpoint),
                "summary": asdict(result),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


class _DryRunDetailAdapter:
    last_page_source: str | None = None

    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        raise RuntimeError("dry-run adapter should not fetch")


if __name__ == "__main__":
    main()
