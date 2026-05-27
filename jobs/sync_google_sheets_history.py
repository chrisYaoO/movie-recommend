from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
import re
from typing import Any, Protocol
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.services.import_service import COLUMN_ALIASES, RAW_HASH_COLUMNS, SOURCE_ROW_NUMBER_KEY, SOURCE_SHEET_KEY
from backend.app.services.matching_service import CachedDoubanSearchAdapter, DoubanHttpSearchAdapter, FileDoubanSearchCache
from backend.app.services.metadata_service import (
    DEFAULT_CHROME_BINARY_PATH,
    DoubanHttpDetailAdapter,
    DoubanSeleniumDetailAdapter,
)
from jobs.import_auto_matched_history import import_auto_matched_rows, resolve_postgres_dsn

DEFAULT_SERVICE_ACCOUNT_FILE = ".secrets/google-sheets-service-account.json"
DEFAULT_SOURCE_FILE_ALIAS = "MOVIES.xlsx"
DEFAULT_DETAIL_ADAPTER = "http"
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


class GoogleSheetsClient(Protocol):
    def values(self, range_name: str) -> list[list[Any]]: ...


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
    parser.add_argument("--sheet", action="append", required=True, help="Sheet tab name. Repeat for multiple tabs.")
    parser.add_argument("--range", default="A1:I", dest="column_range", help="Column range inside each sheet. Defaults to A1:I.")
    parser.add_argument("--source-file-alias", default=DEFAULT_SOURCE_FILE_ALIAS)
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
    rows = read_google_sheet_rows(client, args.sheet, args.column_range)
    if args.dry_run:
        print(json.dumps({"sheet": args.sheet, "row_count": len(rows), "source_file_alias": args.source_file_alias}, ensure_ascii=False, indent=2))
        return

    dsn = resolve_postgres_dsn(args.dsn, args.config_path)
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
        result = import_auto_matched_rows(
            source_file=args.source_file_alias,
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

    print(json.dumps({"summary": asdict(result.summary), "state_path": result.state_path}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
