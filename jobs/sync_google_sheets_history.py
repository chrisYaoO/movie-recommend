from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, NamedTuple, Protocol
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import ConfirmedViewingHistoryInput
from backend.app.services.import_service import (
    COLUMN_ALIASES,
    InMemoryViewingHistoryRawRepository,
    RAW_HASH_COLUMNS,
    SOURCE_ROW_NUMBER_KEY,
    SOURCE_SHEET_KEY,
    ViewingHistoryImportService,
)
from backend.app.services.matching_service import CachedDoubanSearchAdapter, DoubanHttpSearchAdapter, FileDoubanSearchCache
from backend.app.services.metadata_service import (
    DEFAULT_CHROME_BINARY_PATH,
    DoubanHttpDetailAdapter,
    DoubanSeleniumDetailAdapter,
)
from jobs.import_auto_matched_history import import_auto_matched_rows, resolve_postgres_dsn
from jobs.import_auto_matched_history import _progress_row_checksum, _progress_source_sheet_name

DEFAULT_SERVICE_ACCOUNT_FILE = ".secrets/google-sheets-service-account.json"
DEFAULT_SOURCE_NAME = "google-sheets"
DEFAULT_DETAIL_ADAPTER = "http"
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
CONFIRMED_PROGRESS_STATUSES = {
    "auto_matched_persisted",
    "review_confirmed_persisted",
    "manual_id_persisted",
}
CONFIRMED_PROGRESS_STATUS_PRIORITY = {
    "manual_id_persisted": 3,
    "review_confirmed_persisted": 2,
    "auto_matched_persisted": 1,
}


class GoogleSheetsClient(Protocol):
    def values(self, range_name: str) -> list[list[Any]]: ...

    def sheet_names(self) -> list[str]: ...


@dataclass(frozen=True)
class ConfirmedProgressReplaySummary:
    imported_count: int
    skipped_duplicate_count: int
    skipped_invalid_count: int
    mapped_candidate_count: int
    mapping_issue_count: int
    progress_item_count: int
    confirmed_progress_count: int
    matched_confirmed_count: int
    direct_subject_id_count: int
    skipped_without_subject_id_count: int
    persisted_count: int
    existing_count: int
    fetched_count: int
    failed_count: int
    recommendation_discovered_count: int
    recommendation_inserted_count: int
    confirmed_conflict_count: int


class ConfirmedProgressConflict(NamedTuple):
    source_sheet_name: str
    source_row_number: int
    status: str
    subject_ids: tuple[str, ...]


class GoogleSheetsValuesClient:
    def __init__(
        self,
        spreadsheet_id: str,
        api_key: str | None = None,
        bearer_token: str | None = None,
        service_account_file: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.api_key = api_key
        self.bearer_token = bearer_token
        self.service_account_file = service_account_file
        self.timeout_seconds = timeout_seconds

    def values(self, range_name: str) -> list[list[Any]]:
        api_key = None if self.service_account_file else self.api_key
        query = urlencode({"majorDimension": "ROWS", **({"key": api_key} if api_key else {})})
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values/{quote(range_name, safe='!:')}?{query}"
        headers = {"Accept": "application/json"}
        bearer_token = self.bearer_token or _service_account_access_token(self.service_account_file)
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        request = UrlRequest(url, headers=headers)
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("values", [])

    def sheet_names(self) -> list[str]:
        api_key = None if self.service_account_file else self.api_key
        query = urlencode({"fields": "sheets.properties.title", **({"key": api_key} if api_key else {})})
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}?{query}"
        headers = {"Accept": "application/json"}
        bearer_token = self.bearer_token or _service_account_access_token(self.service_account_file)
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        request = UrlRequest(url, headers=headers)
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return [
            str(sheet["properties"]["title"])
            for sheet in payload.get("sheets", [])
            if sheet.get("properties", {}).get("title")
        ]


def read_google_sheet_rows(
    client: GoogleSheetsClient,
    sheet_names: list[str],
    column_range: str = "A:Z",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sheet_name in sheet_names:
        values = client.values(_range_name(sheet_name, column_range))
        rows.extend(_values_to_import_rows(sheet_name, values))
    return rows


def replay_confirmed_progress_rows(
    source_name: str,
    rows: list[dict[str, Any]],
    progress_path: str | Path,
    repository: ViewingHistoryRepository,
    status_writer=None,
) -> ConfirmedProgressReplaySummary:
    import_service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
    import_result = import_service.import_rows(source_name, rows)
    mapping = import_service.to_viewing_history_candidates()
    progress_items = _load_progress_items(progress_path)
    confirmed_by_source_row, confirmed_by_checksum, confirmed_conflicts = _confirmed_subject_ids(progress_items)

    confirmed_inputs: list[ConfirmedViewingHistoryInput] = []
    matched_confirmed_count = 0
    direct_subject_id_count = 0
    skipped_without_subject_id_count = 0

    for candidate in mapping.candidates:
        subject_id = candidate.douban_subject_id
        if subject_id:
            direct_subject_id_count += 1
        else:
            subject_id = confirmed_by_source_row.get((candidate.source_sheet_name, candidate.source_row_number))
            if subject_id:
                matched_confirmed_count += 1
            elif candidate.source_row_checksum:
                subject_id = confirmed_by_checksum.get(candidate.source_row_checksum)
                if subject_id:
                    matched_confirmed_count += 1

        if not subject_id:
            skipped_without_subject_id_count += 1
            continue

        confirmed_inputs.append(
            ConfirmedViewingHistoryInput(
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
        )

    _write_status(
        status_writer,
        "[replay] "
        f"mapped={len(mapping.candidates)}, "
        f"confirmed_progress={len(confirmed_by_source_row)}, "
        f"confirmed_conflicts={len(confirmed_conflicts)}, "
        f"direct_subject_id={direct_subject_id_count}, "
        f"matched_confirmed={matched_confirmed_count}, "
        f"skipped_without_subject_id={skipped_without_subject_id_count}",
    )

    persisted_count = 0
    failed_count = 0
    for confirmed in confirmed_inputs:
        try:
            movie = repository.find_movie_by_subject_id(confirmed.douban_subject_id)
            repository.upsert_viewing_history(confirmed, movie.id if movie is not None else None)
            persisted_count += 1
        except Exception as exc:
            failed_count += 1
            _write_status(
                status_writer,
                "[replay] "
                f"failed source={confirmed.source_sheet_name}:{confirmed.source_row_number}, "
                f"subject={confirmed.douban_subject_id}, error={exc}",
            )
    _write_status(
        status_writer,
        "[replay] "
        f"persisted={persisted_count}, "
        f"failed={failed_count}",
    )
    return ConfirmedProgressReplaySummary(
        imported_count=import_result.imported_count,
        skipped_duplicate_count=import_result.skipped_duplicate_count,
        skipped_invalid_count=import_result.skipped_invalid_count,
        mapped_candidate_count=len(mapping.candidates),
        mapping_issue_count=len(mapping.issues),
        progress_item_count=len(progress_items),
        confirmed_progress_count=len(confirmed_by_source_row),
        matched_confirmed_count=matched_confirmed_count,
        direct_subject_id_count=direct_subject_id_count,
        skipped_without_subject_id_count=skipped_without_subject_id_count,
        persisted_count=persisted_count,
        existing_count=0,
        fetched_count=0,
        failed_count=failed_count,
        recommendation_discovered_count=0,
        recommendation_inserted_count=0,
        confirmed_conflict_count=len(confirmed_conflicts),
    )


def _values_to_import_rows(sheet_name: str, values: list[list[Any]]) -> list[dict[str, Any]]:
    if not values:
        return []
    header = [_canonical_column_name(str(value).strip() if value is not None else None) for value in values[0]]
    rows: list[dict[str, Any]] = []
    for row_number, row_values in enumerate(values[1:], start=2):
        row: dict[str, Any] = {
            column: row_values[index] if index < len(row_values) else None
            for index, column in enumerate(header)
            if column in RAW_HASH_COLUMNS
        }
        row[SOURCE_SHEET_KEY] = sheet_name
        row[SOURCE_ROW_NUMBER_KEY] = row_number
        rows.append(row)
    return rows


def _canonical_column_name(value: str | None) -> str | None:
    if value is None:
        return None
    return COLUMN_ALIASES.get(value, value)


def _range_name(sheet_name: str, column_range: str) -> str:
    if sheet_name.replace("_", "").isalnum():
        return f"{sheet_name}!{column_range}"
    escaped_sheet = sheet_name.replace("'", "''")
    return f"'{escaped_sheet}'!{column_range}"


def _service_account_access_token(service_account_file: str | None) -> str | None:
    if not service_account_file:
        return None
    try:
        from google.auth.transport.requests import Request as AuthRequest
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-auth is required for GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE") from exc

    credentials = service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=(GOOGLE_SHEETS_SCOPE,),
    )
    credentials.refresh(AuthRequest())
    return credentials.token


def _load_progress_items(progress_path: str | Path) -> list[dict[str, Any]]:
    path = Path(progress_path)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("items", []))


def _confirmed_subject_ids(
    items: list[dict[str, Any]],
) -> tuple[dict[tuple[str, int], str], dict[str, str], tuple[ConfirmedProgressConflict, ...]]:
    source_candidates: dict[tuple[str, int], list[tuple[int, str, str]]] = {}
    checksum_candidates: dict[str, list[tuple[int, str, str]]] = {}
    for item in items:
        status = str(item.get("status") or "")
        priority = CONFIRMED_PROGRESS_STATUS_PRIORITY.get(status)
        if priority is None:
            continue
        subject_id = item.get("manual_id_subject_id") or item.get("candidate_subject_id")
        if not subject_id:
            continue
        subject_id = str(subject_id)
        source_sheet_name = _progress_source_sheet_name(item)
        source_row_number = item.get("source_row_number")
        if source_sheet_name and isinstance(source_row_number, int):
            source_candidates.setdefault((source_sheet_name, source_row_number), []).append(
                (priority, status, subject_id)
            )
        source_row_checksum = _progress_row_checksum(item)
        if source_row_checksum:
            checksum_candidates.setdefault(str(source_row_checksum), []).append((priority, status, subject_id))

    by_source_row: dict[tuple[str, int], str] = {}
    conflicts: list[ConfirmedProgressConflict] = []
    for key, candidates in source_candidates.items():
        subject_id, conflict = _select_confirmed_subject_id(candidates)
        if conflict is not None:
            conflicts.append(
                ConfirmedProgressConflict(
                    source_sheet_name=key[0],
                    source_row_number=key[1],
                    status=conflict[0],
                    subject_ids=conflict[1],
                )
            )
            continue
        if subject_id is not None:
            by_source_row[key] = subject_id

    by_checksum: dict[str, str] = {}
    for checksum, candidates in checksum_candidates.items():
        subject_id, conflict = _select_confirmed_subject_id(candidates)
        if conflict is None and subject_id is not None:
            by_checksum[checksum] = subject_id

    return by_source_row, by_checksum, tuple(conflicts)


def _select_confirmed_subject_id(candidates: list[tuple[int, str, str]]) -> tuple[str | None, tuple[str, tuple[str, ...]] | None]:
    if not candidates:
        return None, None
    highest_priority = max(priority for priority, _, _ in candidates)
    highest = [(status, subject_id) for priority, status, subject_id in candidates if priority == highest_priority]
    subject_ids = tuple(dict.fromkeys(subject_id for _, subject_id in highest))
    if len(subject_ids) > 1:
        return None, (highest[0][0], subject_ids)
    return subject_ids[0], None


def _write_status(status_writer, message: str) -> None:
    if status_writer is not None:
        status_writer(message)


def resolve_config_value(config_path: str, key: str) -> str | None:
    return _load_config_value(config_path, key)


def resolve_service_account_file(config_path: str) -> str | None:
    configured = resolve_config_value(config_path, "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE")
    if configured:
        return configured
    if os.path.exists(DEFAULT_SERVICE_ACCOUNT_FILE):
        return DEFAULT_SERVICE_ACCOUNT_FILE
    return None


def resolve_spreadsheet_id(config_path: str) -> str | None:
    secret_id = _spreadsheet_id_from_service_account_file(resolve_service_account_file(config_path))
    if secret_id:
        return secret_id
    configured_id = resolve_config_value(config_path, "GOOGLE_SHEETS_SPREADSHEET_ID")
    return _extract_spreadsheet_id(configured_id)


def _spreadsheet_id_from_service_account_file(path: str | None) -> str | None:
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as file:
        payload = json.load(file)
    for key in ("spreadsheet_id", "google_sheets_spreadsheet_id", "sheet_id"):
        value = payload.get(key)
        extracted = _extract_spreadsheet_id(str(value)) if value else None
        if extracted:
            return extracted
    return None


def _extract_spreadsheet_id(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"/spreadsheets/d/([^/?#]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{25,}", value):
        return value
    return None


def _load_config_value(config_path: str, key: str) -> str | None:
    if not os.path.exists(config_path):
        return None
    with open(config_path, encoding="utf-8") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() != key:
                continue
            return value.strip().strip('"').strip("'") or None
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync viewing history rows from Google Sheets into PostgreSQL.")
    parser.add_argument("--sheet", action="append", help="Sheet tab name. Repeat for multiple tabs. Defaults to every sheet tab.")
    parser.add_argument("--range", default="A1:I", dest="column_range", help="Column range inside each sheet. Defaults to A1:I.")
    parser.add_argument("--source-name", default=DEFAULT_SOURCE_NAME)
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--config-path", default=".env")
    parser.add_argument("--detail-adapter", choices=("http", "selenium"), default=DEFAULT_DETAIL_ADAPTER)
    parser.add_argument("--search-cache-dir", default="data/cache/douban-search")
    parser.add_argument("--resume-state-path", default="data/cache/import-auto-match-progress.json")
    parser.add_argument("--chrome-binary-path", default=DEFAULT_CHROME_BINARY_PATH)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Read Google Sheets rows and print import counts without writing.")
    parser.add_argument(
        "--replay-confirmed-progress",
        action="store_true",
        help="Rebuild viewing_history from Google Sheets rows matched to confirmed progress JSON subject IDs.",
    )
    args = parser.parse_args()

    spreadsheet_id = resolve_spreadsheet_id(args.config_path)
    service_account_file = resolve_service_account_file(args.config_path)
    api_key = resolve_config_value(args.config_path, "GOOGLE_SHEETS_API_KEY")
    bearer_token = resolve_config_value(args.config_path, "GOOGLE_SHEETS_ACCESS_TOKEN")

    if not spreadsheet_id:
        parser.error(f"GOOGLE_SHEETS_SPREADSHEET_ID is required in {args.config_path}")

    client = GoogleSheetsValuesClient(
        spreadsheet_id=spreadsheet_id,
        api_key=None if service_account_file else api_key,
        bearer_token=None if service_account_file else bearer_token,
        service_account_file=service_account_file,
        timeout_seconds=args.timeout_seconds,
    )
    sheet_names = args.sheet or client.sheet_names()
    rows = read_google_sheet_rows(client, sheet_names, args.column_range)
    if args.dry_run:
        progress_items = _load_progress_items(args.resume_state_path) if args.replay_confirmed_progress else []
        confirmed_by_source_row, _, confirmed_conflicts = _confirmed_subject_ids(progress_items)
        print(
            json.dumps(
                {
                    "sheet": sheet_names,
                    "row_count": len(rows),
                    "source_name": args.source_name,
                    "progress_item_count": len(progress_items),
                    "confirmed_progress_count": len(confirmed_by_source_row),
                    "confirmed_conflict_count": len(confirmed_conflicts),
                    "confirmed_conflicts": [conflict._asdict() for conflict in confirmed_conflicts],
                    "mode": "replay-confirmed-progress" if args.replay_confirmed_progress else "auto-match",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    dsn = resolve_postgres_dsn(args.dsn, args.config_path)
    repository = PostgresViewingHistoryRepository(dsn)
    repository.initialize_schema()
    detail_adapter = None
    if not args.replay_confirmed_progress:
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
        if args.replay_confirmed_progress:
            result = replay_confirmed_progress_rows(
                source_name=args.source_name,
                rows=rows[: args.limit] if args.limit is not None else rows,
                progress_path=args.resume_state_path,
                repository=repository,
                status_writer=lambda message: print(message, flush=True),
            )
        else:
            search_adapter = CachedDoubanSearchAdapter(
                DoubanHttpSearchAdapter(timeout_seconds=args.timeout_seconds, delay_seconds=args.delay_seconds),
                FileDoubanSearchCache(args.search_cache_dir),
            )
            result = import_auto_matched_rows(
                source_sheet_name=args.source_name,
                rows=rows,
                search_adapter=search_adapter,
                detail_adapter=detail_adapter,
                repository=repository,
                state_path=args.resume_state_path,
                limit=args.limit,
                status_writer=lambda message: print(message, flush=True),
            )
    finally:
        if hasattr(detail_adapter, "close"):
            detail_adapter.close()
        repository.close()

    if isinstance(result, ConfirmedProgressReplaySummary):
        payload = {"summary": asdict(result), "state_path": args.resume_state_path}
    else:
        payload = {"summary": asdict(result.summary), "state_path": result.state_path}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()


