from __future__ import annotations

from dataclasses import dataclass
import json
import re
from threading import Lock
import time
from typing import Any, Callable, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from backend.app.config import GOOGLE_SHEETS_SCOPE


class GoogleSheetsAppendService(Protocol):
    def append_viewing_history_row(self, sheet_name: str, values: list[Any]) -> "AppendSheetRowResult": ...


@dataclass(frozen=True)
class AppendSheetRowResult:
    sheet_name: str
    row_number: int
    updated_range: str


@dataclass(frozen=True)
class SheetHistoryProjection:
    history_id: str
    sheet_name: str
    values: list[Any]


@dataclass(frozen=True)
class SheetHistoryLocation:
    sheet_name: str
    row_number: int
    updated_range: str


class DuplicateRecordIdError(ValueError):
    pass


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


class GoogleSheetsHistoryService(GoogleSheetsValuesAppendService):
    """Projects one local history row to A:J, with its UUID fixed in column J."""

    def upsert_history_row(
        self,
        projection: SheetHistoryProjection,
        hinted_sheet_name: str | None = None,
        hinted_row_number: int | None = None,
    ) -> SheetHistoryLocation:
        if len(projection.values) != 9:
            raise ValueError("viewing-history projection must contain exactly the A:I values")
        self._ensure_record_id_column(projection.sheet_name)

        hinted = self._validated_hint(
            projection.history_id,
            hinted_sheet_name,
            hinted_row_number,
        )
        if hinted and hinted.sheet_name == projection.sheet_name:
            return self._update_projection(projection, hinted.row_number)

        if hinted:
            target_matches = self._find_record_id(projection.history_id, (projection.sheet_name,))
            if len(target_matches) > 1:
                raise DuplicateRecordIdError(f"duplicate RecordId: {projection.history_id}")
            target = (
                self._update_projection(projection, target_matches[0].row_number)
                if target_matches
                else self._append_projection(projection)
            )
            # Cross-year moves are deliberately target-first so a failure cannot lose the row.
            self._delete_sheet_row(hinted.sheet_name, hinted.row_number, projection.history_id)
            return target

        matches = self._find_record_id(projection.history_id)
        if len(matches) > 1:
            raise DuplicateRecordIdError(f"duplicate RecordId: {projection.history_id}")
        if not matches:
            return self._append_projection(projection)
        match = matches[0]
        if match.sheet_name == projection.sheet_name:
            return self._update_projection(projection, match.row_number)

        target = self._append_projection(projection)
        self._delete_sheet_row(match.sheet_name, match.row_number, projection.history_id)
        return target

    def delete_history_row(
        self,
        history_id: str,
        hinted_sheet_name: str | None = None,
        hinted_row_number: int | None = None,
    ) -> bool:
        hinted = self._validated_hint(history_id, hinted_sheet_name, hinted_row_number)
        if hinted:
            self._delete_sheet_row(hinted.sheet_name, hinted.row_number, history_id)
            return True
        matches = self._find_record_id(history_id)
        if len(matches) > 1:
            raise DuplicateRecordIdError(f"duplicate RecordId: {history_id}")
        if not matches:
            return False
        self._delete_sheet_row(matches[0].sheet_name, matches[0].row_number, history_id)
        return True

    def read_history_rows(self) -> dict[str, list[list[Any]]]:
        return {
            title: self._get_values(_range_name(title, "A:J"))
            for title in self._history_sheet_ids()
        }

    def backfill_record_id(self, sheet_name: str, row_number: int, history_id: str) -> SheetHistoryLocation:
        if row_number < 2:
            raise ValueError("cannot backfill RecordId into a header row")
        self._ensure_record_id_column(sheet_name)
        range_name = _range_name(sheet_name, f"J{row_number}")
        current = self._get_values(range_name)
        value = str(current[0][0]).strip() if current and current[0] else ""
        if value and value != history_id:
            raise ValueError(f"RecordId conflict at {sheet_name}:{row_number}")
        updated_range = range_name if value else self._put_values(range_name, [[history_id]])
        return SheetHistoryLocation(sheet_name, row_number, updated_range or range_name)

    def _ensure_record_id_column(self, sheet_name: str) -> None:
        prepared = getattr(self, "_prepared_record_id_sheets", set())
        if sheet_name in prepared:
            return
        sheet_ids = self._sheet_ids()
        if sheet_name not in sheet_ids:
            raise ValueError(f"Google Sheets tab does not exist: {sheet_name}")
        header = self._get_values(_range_name(sheet_name, "A1:Z1"))
        cells = header[0] if header else []
        positions = [index for index, value in enumerate(cells) if str(value).strip() == "RecordId"]
        if positions and positions != [9]:
            raise ValueError(f"RecordId must appear exactly once in column J on {sheet_name}")
        if len(cells) > 9 and str(cells[9]).strip() not in {"", "RecordId"}:
            raise ValueError(f"column J on {sheet_name} is already user-managed")
        if not positions:
            self._put_values(_range_name(sheet_name, "J1"), [["RecordId"]])
        self._hide_record_id_column(sheet_ids[sheet_name])
        prepared.add(sheet_name)
        self._prepared_record_id_sheets = prepared

    def _validated_hint(
        self,
        history_id: str,
        sheet_name: str | None,
        row_number: int | None,
    ) -> SheetHistoryLocation | None:
        if not sheet_name or not row_number or row_number < 2:
            return None
        if sheet_name not in self._sheet_ids():
            return None
        values = self._get_values(_range_name(sheet_name, f"J{row_number}"))
        if values and values[0] and str(values[0][0]).strip() == history_id:
            return SheetHistoryLocation(sheet_name, row_number, _range_name(sheet_name, f"A{row_number}:J{row_number}"))
        return None

    def _find_record_id(
        self,
        history_id: str,
        sheet_names: tuple[str, ...] | None = None,
    ) -> list[SheetHistoryLocation]:
        names = sheet_names or tuple(self._history_sheet_ids())
        matches: list[SheetHistoryLocation] = []
        for sheet_name in names:
            for index, row in enumerate(self._get_values(_range_name(sheet_name, "J2:J")), start=2):
                if row and str(row[0]).strip() == history_id:
                    matches.append(
                        SheetHistoryLocation(sheet_name, index, _range_name(sheet_name, f"A{index}:J{index}"))
                    )
        return matches

    def _history_sheet_ids(self) -> dict[str, int]:
        return {title: sheet_id for title, sheet_id in self._sheet_ids().items() if re.fullmatch(r"\d{4}", title)}

    def _update_projection(self, projection: SheetHistoryProjection, row_number: int) -> SheetHistoryLocation:
        range_name = _range_name(projection.sheet_name, f"A{row_number}:J{row_number}")
        updated_range = self._put_values(range_name, [[*projection.values, projection.history_id]])
        return SheetHistoryLocation(projection.sheet_name, row_number, updated_range or range_name)

    def _append_projection(self, projection: SheetHistoryProjection) -> SheetHistoryLocation:
        result = self._append_values(
            _range_name(projection.sheet_name, "A:J"),
            [[*projection.values, projection.history_id]],
        )
        return SheetHistoryLocation(
            projection.sheet_name,
            _row_number_from_updated_range(result),
            result,
        )

    def _sheet_ids(self) -> dict[str, int]:
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
            "?fields=sheets.properties(sheetId,title)"
        )
        payload = self._request_json("GET", url)
        return {
            str(item["properties"]["title"]): int(item["properties"]["sheetId"])
            for item in payload.get("sheets", [])
            if item.get("properties", {}).get("title") is not None
        }

    def _get_values(self, range_name: str) -> list[list[Any]]:
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
            f"/values/{quote(range_name, safe='!:')}"
        )
        return list(self._request_json("GET", url).get("values", []))

    def _put_values(self, range_name: str, values: list[list[Any]]) -> str:
        query = urlencode({"valueInputOption": "USER_ENTERED"})
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
            f"/values/{quote(range_name, safe='!:')}?{query}"
        )
        payload = self._request_json("PUT", url, {"values": values})
        return str(payload.get("updatedRange") or "")

    def _append_values(self, range_name: str, values: list[list[Any]]) -> str:
        query = urlencode({"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"})
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
            f"/values/{quote(range_name, safe='!:')}:append?{query}"
        )
        payload = self._request_json("POST", url, {"values": values})
        updated_range = str(payload.get("updates", {}).get("updatedRange") or "")
        _row_number_from_updated_range(updated_range)
        return updated_range

    def _hide_record_id_column(self, sheet_id: int) -> None:
        self._batch_update({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 9, "endIndex": 10},
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }
        })

    def _delete_sheet_row(self, sheet_name: str, row_number: int, history_id: str) -> None:
        sheet_id = self._sheet_ids().get(sheet_name)
        if sheet_id is None:
            raise ValueError(f"Google Sheets tab does not exist: {sheet_name}")
        current = self._get_values(_range_name(sheet_name, f"J{row_number}"))
        if not current or not current[0] or str(current[0][0]).strip() != history_id:
            raise ValueError(f"RecordId validation failed before deleting {sheet_name}:{row_number}")
        self._batch_update({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": row_number - 1,
                    "endIndex": row_number,
                }
            }
        })

    def _batch_update(self, request: dict[str, Any]) -> None:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}:batchUpdate"
        self._request_json("POST", url, {"requests": [request]})

    def _request_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        force_refresh: bool = False,
        retry_count: int = 0,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = UrlRequest(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._access_token(force_refresh=force_refresh)}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 401 and not force_refresh:
                return self._request_json(method, url, payload, force_refresh=True, retry_count=retry_count)
            if exc.code == 429 and retry_count < 7:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = float(retry_after) if retry_after is not None else min(2**retry_count, 32)
                except ValueError:
                    delay = min(2**retry_count, 32)
                time.sleep(max(delay, 0))
                return self._request_json(method, url, payload, force_refresh=force_refresh, retry_count=retry_count + 1)
            raise


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
