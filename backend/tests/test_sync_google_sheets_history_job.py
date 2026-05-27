import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from backend.app.services.import_service import InMemoryViewingHistoryRawRepository, ViewingHistoryImportService
from jobs.sync_google_sheets_history import (
    DEFAULT_DETAIL_ADAPTER,
    DEFAULT_SOURCE_FILE_ALIAS,
    GOOGLE_SHEETS_SCOPE,
    GoogleSheetsValuesClient,
    _range_name,
    read_google_sheet_rows,
    resolve_config_value,
    resolve_service_account_file,
    resolve_spreadsheet_id,
)


class SyncGoogleSheetsHistoryJobTest(unittest.TestCase):
    def test_reads_google_values_as_excel_compatible_source_rows(self) -> None:
        client = _FakeSheetsClient(
            {
                "2026!A:Z": [
                    ["Date", "Name", "Director", "Year", "Ratings", "Quality", "Comments", "movie_id", "image_id"],
                    ["2026-05-12", "Yi Yi", "Edward Yang", 2000, 5.0, "1080p", "favorite", 1291561, 456789],
                    ["2026-05-13", "No Rating", "Nobody", 2001, None, "1080p", "skip", None, None],
                ]
            }
        )

        rows = read_google_sheet_rows(client, ["2026"])
        service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
        result = service.import_rows("MOVIES.xlsx", rows)

        self.assertEqual(["2026!A:Z"], client.ranges)
        self.assertEqual(1, result.imported_count)
        self.assertEqual(1, result.skipped_invalid_count)
        imported = result.rows[0]
        self.assertEqual("MOVIES.xlsx#2026", imported.source_file)
        self.assertEqual(2, imported.source_row_number)
        self.assertEqual("Yi Yi", imported.name_raw)
        self.assertEqual("5.0", imported.rating_raw)
        self.assertEqual("1291561", imported.douban_subject_id_raw)
        self.assertEqual("456789", imported.douban_image_id_raw)

    def test_quotes_sheet_names_in_range(self) -> None:
        self.assertEqual("'Movie Reviews'!A:Z", _range_name("Movie Reviews", "A:Z"))
        self.assertEqual("'Director''s Cut'!A:J", _range_name("Director's Cut", "A:J"))
        self.assertEqual("2026!A1:I", _range_name("2026", "A1:I"))

    def test_resolves_google_config_from_config_file_only(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / ".env"
            config_path.write_text("GOOGLE_SHEETS_API_KEY=from-file\n", encoding="utf-8")

            self.assertEqual("from-file", resolve_config_value(str(config_path), "GOOGLE_SHEETS_API_KEY"))

    def test_service_account_file_defaults_to_local_secrets_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / ".secrets"
            secrets.mkdir()
            (secrets / "google-sheets-service-account.json").write_text("{}", encoding="utf-8")
            config_path = root / ".env"
            config_path.write_text("", encoding="utf-8")

            current = Path.cwd()
            try:
                os.chdir(root)
                self.assertEqual(
                    ".secrets/google-sheets-service-account.json",
                    resolve_service_account_file(str(config_path)),
                )
            finally:
                os.chdir(current)

    def test_resolves_spreadsheet_id_from_service_account_json(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / ".secrets"
            secrets.mkdir()
            (secrets / "google-sheets-service-account.json").write_text(
                '{"spreadsheet_id": "spreadsheet-id-with-realistic-length"}',
                encoding="utf-8",
            )
            config_path = root / ".env"
            config_path.write_text("", encoding="utf-8")

            current = Path.cwd()
            try:
                os.chdir(root)
                self.assertEqual("spreadsheet-id-with-realistic-length", resolve_spreadsheet_id(str(config_path)))
            finally:
                os.chdir(current)

    def test_service_account_spreadsheet_id_takes_priority_over_env(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / ".secrets"
            secrets.mkdir()
            (secrets / "google-sheets-service-account.json").write_text(
                '{"spreadsheet_id": "json-id-with-realistic-length"}',
                encoding="utf-8",
            )
            config_path = root / ".env"
            config_path.write_text(
                "GOOGLE_SHEETS_SPREADSHEET_ID=https://docs.google.com/spreadsheets/d/url-id/edit\n",
                encoding="utf-8",
            )

            current = Path.cwd()
            try:
                os.chdir(root)
                self.assertEqual("json-id-with-realistic-length", resolve_spreadsheet_id(str(config_path)))
            finally:
                os.chdir(current)

    def test_service_account_token_is_used_as_authorization_header(self) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b'{"values": [["Name"], ["Yi Yi"]]}'

        with patch("jobs.sync_google_sheets_history._service_account_access_token", return_value="token") as token, patch(
            "jobs.sync_google_sheets_history.urlopen", return_value=response
        ) as opened:
            client = GoogleSheetsValuesClient(
                spreadsheet_id="spreadsheet",
                api_key="ignored-api-key",
                service_account_file="movie-491021-1cd922995007.json",
            )

            values = client.values("'2026'!A:Z")

        request = opened.call_args.args[0]
        self.assertEqual([["Name"], ["Yi Yi"]], values)
        self.assertEqual("token", token.return_value)
        self.assertEqual("Bearer token", request.headers["Authorization"])
        self.assertNotIn("ignored-api-key", request.full_url)

    def test_service_account_scope_allows_future_sheet_writes(self) -> None:
        self.assertEqual("https://www.googleapis.com/auth/spreadsheets", GOOGLE_SHEETS_SCOPE)

    def test_sync_defaults_keep_common_command_short(self) -> None:
        self.assertEqual("MOVIES.xlsx", DEFAULT_SOURCE_FILE_ALIAS)
        self.assertEqual("http", DEFAULT_DETAIL_ADAPTER)


class _FakeSheetsClient:
    def __init__(self, values_by_range):
        self.values_by_range = values_by_range
        self.ranges = []

    def values(self, range_name):
        self.ranges.append(range_name)
        return self.values_by_range[range_name]


if __name__ == "__main__":
    unittest.main()
