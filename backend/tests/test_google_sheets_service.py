import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.app.services.google_sheets_service import GoogleSheetsValuesAppendService, _row_number_from_updated_range


class GoogleSheetsServiceTest(unittest.TestCase):
    def test_parses_row_number_from_updated_range(self) -> None:
        self.assertEqual(27, _row_number_from_updated_range("2026!A27:I27"))
        self.assertEqual(27, _row_number_from_updated_range("'Movie Reviews'!A27:I27"))

    def test_rejects_updated_range_without_row_number(self) -> None:
        with self.assertRaisesRegex(ValueError, "updatedRange"):
            _row_number_from_updated_range("2026!A:I")

    def test_reuses_valid_service_account_token_across_appends(self) -> None:
        credentials = _FakeCredentials()
        service = GoogleSheetsValuesAppendService(
            spreadsheet_id="sheet-id",
            service_account_file="credentials.json",
            credentials_factory=lambda _: credentials,
            auth_request_factory=lambda: object(),
        )

        with patch(
            "backend.app.services.google_sheets_service.urlopen",
            side_effect=[_FakeResponse("2026!A27:I27"), _FakeResponse("2026!A28:I28")],
        ):
            first = service.append_viewing_history_row("2026", ["first"])
            second = service.append_viewing_history_row("2026", ["second"])

        self.assertEqual(27, first.row_number)
        self.assertEqual(28, second.row_number)
        self.assertEqual(1, credentials.refresh_count)

    def test_refreshes_token_and_retries_once_after_unauthorized_response(self) -> None:
        credentials = _FakeCredentials(token="cached-token", valid=True)
        service = GoogleSheetsValuesAppendService(
            spreadsheet_id="sheet-id",
            service_account_file="credentials.json",
            credentials_factory=lambda _: credentials,
            auth_request_factory=lambda: object(),
        )
        unauthorized = HTTPError("https://sheets.googleapis.com", 401, "Unauthorized", {}, None)

        with patch(
            "backend.app.services.google_sheets_service.urlopen",
            side_effect=[unauthorized, _FakeResponse("2026!A29:I29")],
        ) as urlopen:
            result = service.append_viewing_history_row("2026", ["value"])

        self.assertEqual(29, result.row_number)
        self.assertEqual(1, credentials.refresh_count)
        self.assertEqual(2, urlopen.call_count)


class _FakeCredentials:
    def __init__(self, token=None, valid=False) -> None:
        self.token = token
        self.valid = valid
        self.refresh_count = 0

    def refresh(self, _request) -> None:
        self.refresh_count += 1
        self.token = f"token-{self.refresh_count}"
        self.valid = True


class _FakeResponse:
    status = 200

    def __init__(self, updated_range: str) -> None:
        self.updated_range = updated_range

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass

    def read(self) -> bytes:
        return ('{"updates":{"updatedRange":"' + self.updated_range + '"}}').encode("utf-8")


if __name__ == "__main__":
    unittest.main()
