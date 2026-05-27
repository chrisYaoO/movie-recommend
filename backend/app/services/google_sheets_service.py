from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Protocol
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from jobs.sync_google_sheets_history import _service_account_access_token


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
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.service_account_file = service_account_file
        self.timeout_seconds = timeout_seconds

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
        request = UrlRequest(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {_service_account_access_token(self.service_account_file)}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        updated_range = str(payload.get("updates", {}).get("updatedRange") or "")
        return AppendSheetRowResult(
            sheet_name=sheet_name,
            row_number=_row_number_from_updated_range(updated_range),
            updated_range=updated_range,
        )


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
