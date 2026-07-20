import os
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from backend.app.config import (
    GOOGLE_SHEETS_SCOPE,
    config_value,
    resolve_service_account_file,
    resolve_spreadsheet_id,
)
from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.services.import_service import InMemoryViewingHistoryRawRepository, ViewingHistoryImportService
from jobs.sync_google_sheets_history import (
    DEFAULT_DETAIL_ADAPTER,
    DEFAULT_SOURCE_NAME,
    GoogleSheetsValuesClient,
    _confirmed_subject_ids,
    _range_name,
    read_google_sheet_rows,
    replay_confirmed_progress_rows,
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
        self.assertEqual("2026", imported.source_sheet_name)
        self.assertEqual(2, imported.source_row_number)
        self.assertEqual("Yi Yi", imported.name_raw)
        self.assertEqual("5.0", imported.rating_raw)
        self.assertEqual("1291561", imported.douban_subject_id_raw)
        self.assertEqual("456789", imported.douban_image_id_raw)

    def test_month_day_sheet_dates_inherit_the_year_tab(self) -> None:
        client = _FakeSheetsClient(
            {
                "2026!A:Z": [
                    ["Date", "Name", "Director", "Year", "Rating"],
                    ["1/2", "Yi Yi", "Edward Yang", 2000, 5.0],
                ]
            }
        )

        rows = read_google_sheet_rows(client, ["2026"])
        service = ViewingHistoryImportService(InMemoryViewingHistoryRawRepository())
        service.import_rows("google-sheets", rows)

        self.assertEqual(date(2026, 1, 2), service.to_viewing_history_candidates().candidates[0].watched_date)

    def test_replay_preserves_sheet_record_id(self) -> None:
        history_id = "2aaae72e-6c6e-43b8-959b-b3584a68b718"
        client = _FakeSheetsClient(
            {
                "2026!A:Z": [
                    ["Date", "Name", "Director", "Year", "Rating", "RecordId"],
                    ["1/2", "Yi Yi", "Edward Yang", 2000, 5.0, history_id],
                ]
            }
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            progress_path = root / "progress.json"
            progress_path.write_text('{"items": []}', encoding="utf-8")
            rows = read_google_sheet_rows(client, ["2026"])
            rows[0]["DoubanSubjectId"] = "1291561"
            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                replay_confirmed_progress_rows("google-sheets", rows, progress_path, repository)
                history = repository.connection.execute(
                    "SELECT id, watched_date FROM viewing_history"
                ).fetchone()

        self.assertEqual((history_id, "2026-01-02"), tuple(history))

    def test_quotes_sheet_names_in_range(self) -> None:
        self.assertEqual("'Movie Reviews'!A:Z", _range_name("Movie Reviews", "A:Z"))
        self.assertEqual("'Director''s Cut'!A:J", _range_name("Director's Cut", "A:J"))
        self.assertEqual("2026!A1:I", _range_name("2026", "A1:I"))

    def test_resolves_google_config_from_config_file_only(self) -> None:
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / ".env"
            config_path.write_text("GOOGLE_SHEETS_API_KEY=from-file\n", encoding="utf-8")

            self.assertEqual("from-file", config_value(str(config_path), "GOOGLE_SHEETS_API_KEY"))

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

    def test_reads_sheet_names_from_spreadsheet_metadata(self) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b'{"sheets": [{"properties": {"title": "2025"}}, {"properties": {"title": "2026"}}]}'

        with patch("jobs.sync_google_sheets_history._service_account_access_token", return_value="token"), patch(
            "jobs.sync_google_sheets_history.urlopen", return_value=response
        ) as opened:
            client = GoogleSheetsValuesClient(
                spreadsheet_id="spreadsheet",
                api_key="ignored-api-key",
                service_account_file="movie-491021-1cd922995007.json",
            )

            names = client.sheet_names()

        request = opened.call_args.args[0]
        self.assertEqual(["2025", "2026"], names)
        self.assertIn("fields=sheets.properties.title", request.full_url)
        self.assertNotIn("ignored-api-key", request.full_url)

    def test_service_account_scope_allows_future_sheet_writes(self) -> None:
        self.assertEqual("https://www.googleapis.com/auth/spreadsheets", GOOGLE_SHEETS_SCOPE)

    def test_sync_defaults_keep_common_command_short(self) -> None:
        self.assertEqual("google-sheets", DEFAULT_SOURCE_NAME)
        self.assertEqual("http", DEFAULT_DETAIL_ADAPTER)

    def test_confirmed_progress_uses_legacy_source_file_sheet_name(self) -> None:
        by_source_row, by_checksum, conflicts = _confirmed_subject_ids(
            [
                {
                    "source_file": "MOVIES.xlsx#2026",
                    "source_row_number": 2,
                    "source_row_hash": "old-checksum",
                    "candidate_subject_id": "1291561",
                    "status": "review_confirmed_persisted",
                }
            ]
        )

        self.assertEqual({("2026", 2): "1291561"}, by_source_row)
        self.assertEqual({"old-checksum": "1291561"}, by_checksum)
        self.assertEqual((), conflicts)

    def test_confirmed_progress_uses_explicit_status_priority(self) -> None:
        by_source_row, _, conflicts = _confirmed_subject_ids(
            [
                {
                    "source_sheet_name": "2026",
                    "source_row_number": 2,
                    "candidate_subject_id": "auto-id",
                    "status": "auto_matched_persisted",
                },
                {
                    "source_sheet_name": "2026",
                    "source_row_number": 2,
                    "candidate_subject_id": "review-id",
                    "status": "review_confirmed_persisted",
                },
                {
                    "source_sheet_name": "2026",
                    "source_row_number": 2,
                    "manual_id_subject_id": "manual-id",
                    "candidate_subject_id": "candidate-id",
                    "status": "manual_id_persisted",
                },
            ]
        )

        self.assertEqual({("2026", 2): "manual-id"}, by_source_row)
        self.assertEqual((), conflicts)

    def test_confirmed_progress_reports_same_priority_subject_conflict(self) -> None:
        by_source_row, _, conflicts = _confirmed_subject_ids(
            [
                {
                    "source_sheet_name": "2026",
                    "source_row_number": 2,
                    "manual_id_subject_id": "manual-1",
                    "status": "manual_id_persisted",
                },
                {
                    "source_sheet_name": "2026",
                    "source_row_number": 2,
                    "manual_id_subject_id": "manual-2",
                    "status": "manual_id_persisted",
                },
                {
                    "source_sheet_name": "2026",
                    "source_row_number": 3,
                    "candidate_subject_id": "review-id",
                    "status": "review_confirmed_persisted",
                },
            ]
        )

        self.assertEqual({("2026", 3): "review-id"}, by_source_row)
        self.assertEqual(1, len(conflicts))
        self.assertEqual("2026", conflicts[0].source_sheet_name)
        self.assertEqual(2, conflicts[0].source_row_number)
        self.assertEqual("manual_id_persisted", conflicts[0].status)
        self.assertEqual(("manual-1", "manual-2"), conflicts[0].subject_ids)

    def test_replays_confirmed_progress_rows_without_fetching_movie_details(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            progress_path = root / "progress.json"
            progress_path.write_text(
                """{
                  "items": [
                    {
                      "source_file": "MOVIES.xlsx#2026",
                      "source_row_number": 2,
                      "candidate_subject_id": "1291561",
                      "status": "review_confirmed_persisted"
                    }
                  ]
                }""",
                encoding="utf-8",
            )
            rows = [
                {
                    "__source_sheet": "2026",
                    "__source_row_number": 2,
                    "Date": "2026-05-12",
                    "Name": "Yi Yi",
                    "Director": "Edward Yang",
                    "Year": 2000,
                    "Rating": 5.0,
                    "Quality": "1080p",
                    "Comment": "favorite",
                }
            ]
            with SQLiteViewingHistoryRepository(root / "movies.db") as repository:
                repository.initialize_schema()
                summary = replay_confirmed_progress_rows(
                    source_name="google-sheets",
                    rows=rows,
                    progress_path=progress_path,
                    repository=repository,
                )
                movie_count = repository.connection.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
                history = repository.connection.execute(
                    "SELECT source_sheet_name, source_row_number, douban_subject_id, movie_id FROM viewing_history"
                ).fetchone()

        self.assertEqual(1, summary.matched_confirmed_count)
        self.assertEqual(1, summary.persisted_count)
        self.assertEqual(0, summary.confirmed_conflict_count)
        self.assertEqual(0, summary.fetched_count)
        self.assertEqual(0, summary.recommendation_inserted_count)
        self.assertEqual(0, movie_count)
        self.assertEqual(("2026", 2, "1291561", None), tuple(history))


class _FakeSheetsClient:
    def __init__(self, values_by_range):
        self.values_by_range = values_by_range
        self.ranges = []

    def values(self, range_name):
        self.ranges.append(range_name)
        return self.values_by_range[range_name]

    def sheet_names(self):
        return ["2026"]


if __name__ == "__main__":
    unittest.main()


