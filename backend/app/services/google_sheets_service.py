from __future__ import annotations

from dataclasses import dataclass
import json
import re
from threading import Lock
from typing import Any, Callable, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from jobs.sync_google_sheets_history import GOOGLE_SHEETS_SCOPE


class GoogleSheetsAppendService(Protocol):
    def append_viewing_history_row(self, sheet_name: str, values: list[Any]) -> "AppendSheetRowResult": ...


@dataclass(frozen=True)
class AppendSheetRowResult:
    sheet_name: str
    row_number: int
    updated_range: str


class GoogleSheetsValuesAppendService:
    def __init__(
        self,
        spreadsheet_id: str,
        service_account_file: str,
        timeout_seconds: float = 20.0,
        credentials_factory: Callable[[str], Any] | None = None,
        auth_request_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.service_account_file = service_account_file
        self.timeout_seconds = timeout_seconds
        self.credentials_factory = credentials_factory or _load_service_account_credentials
        self.auth_request_factory = auth_request_factory or _create_auth_request
        self.credentials = None
        self.credentials_lock = Lock()

    def append_viewing_history_row(self, sheet_name: str, values: list[Any]) -> AppendSheetRowResult:
        range_name = _range_name(sheet_name, "A:I")
        query = urlencode(
            {
                "valueInputOption": "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS",
                "includeValuesInResponse": "false",
            }
        )
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
            f"/values/{quote(range_name, safe='!:')}:append?{query}"
        )
        body = json.dumps({"values": [values]}, ensure_ascii=False).encode("utf-8")
        try:
            payload = self._append(url, body)
        except HTTPError as exc:
            if exc.code != 401:
                raise
            payload = self._append(url, body, force_refresh=True)
        updated_range = str(payload.get("updates", {}).get("updatedRange") or "")
        return AppendSheetRowResult(
            sheet_name=sheet_name,
            row_number=_row_number_from_updated_range(updated_range),
            updated_range=updated_range,
        )

    def _append(self, url: str, body: bytes, force_refresh: bool = False) -> dict[str, Any]:
        request = UrlRequest(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._access_token(force_refresh=force_refresh)}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _access_token(self, force_refresh: bool = False) -> str:
        with self.credentials_lock:
            if self.credentials is None:
                self.credentials = self.credentials_factory(self.service_account_file)
            if force_refresh or not self.credentials.valid:
                self.credentials.refresh(self.auth_request_factory())
            if not self.credentials.token:
                raise RuntimeError("Google Sheets service-account token is unavailable")
            return str(self.credentials.token)


def _load_service_account_credentials(service_account_file: str):
    try:
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("google-auth is required for GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE") from exc
    return service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=(GOOGLE_SHEETS_SCOPE,),
    )


def _create_auth_request():
    try:
        from google.auth.transport.requests import Request as AuthRequest
    except ImportError as exc:
        raise RuntimeError("google-auth is required for GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE") from exc
    return AuthRequest()


def _range_name(sheet_name: str, column_range: str) -> str:
    if sheet_name.replace("_", "").isalnum():
        return f"{sheet_name}!{column_range}"
    escaped_sheet = sheet_name.replace("'", "''")
    return f"'{escaped_sheet}'!{column_range}"


def _row_number_from_updated_range(updated_range: str) -> int:
    match = re.search(r"![A-Z]+(\d+):", updated_range)
    if match is None:
        match = re.search(r"![A-Z]+(\d+)$", updated_range)
    if match is None:
        raise ValueError(f"Could not parse appended row number from updatedRange: {updated_range}")
    return int(match.group(1))
