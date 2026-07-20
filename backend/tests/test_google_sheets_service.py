import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.app.services.google_sheets_service import (
    DuplicateRecordIdError,
    GoogleSheetsHistoryService,
    GoogleSheetsValuesAppendService,
    SheetHistoryProjection,
    _row_number_from_updated_range,
)


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

    def test_retries_rate_limit_using_retry_after(self) -> None:
        credentials = _FakeCredentials(token="cached-token", valid=True)
        service = GoogleSheetsHistoryService(
            spreadsheet_id="sheet-id",
            service_account_file="credentials.json",
            credentials_factory=lambda _: credentials,
            auth_request_factory=lambda: object(),
        )
        rate_limited = HTTPError(
            "https://sheets.googleapis.com",
            429,
            "Too Many Requests",
            {"Retry-After": "0"},
            None,
        )

        with patch(
            "backend.app.services.google_sheets_service.urlopen",
            side_effect=[rate_limited, _FakeJsonResponse({"values": [["ok"]]})],
        ) as urlopen:
            result = service._request_json("GET", "https://sheets.googleapis.com/test")

        self.assertEqual({"values": [["ok"]]}, result)
        self.assertEqual(2, urlopen.call_count)


class MemoryHistorySheets(GoogleSheetsHistoryService):
    def __init__(self, rows=None):
        super().__init__("spreadsheet", "credentials.json")
        self.rows = rows or {"2025": [["h"] * 9], "2026": [["h"] * 9]}
        self.events = []
        self.fail_after_append = False

    def _sheet_ids(self):
        return {name: index for index, name in enumerate(self.rows, start=1)}

    def _get_values(self, range_name):
        sheet, cells = range_name.replace("'", "").split("!", 1)
        rows = self.rows[sheet]
        if cells == "A1:Z1":
            return [rows[0]] if rows else []
        if cells == "A:J":
            return rows
        if cells == "J2:J":
            return [[row[9]] if len(row) > 9 else [] for row in rows[1:]]
        if cells.startswith("J"):
            row_number = int(cells[1:])
            if row_number > len(rows) or len(rows[row_number - 1]) <= 9:
                return []
            return [[rows[row_number - 1][9]]]
        raise AssertionError(range_name)

    def _put_values(self, range_name, values):
        sheet, cells = range_name.replace("'", "").split("!", 1)
        if cells == "J1":
            self.rows[sheet][0] = [*self.rows[sheet][0][:9], "RecordId"]
        elif cells.startswith("J"):
            row_number = int(cells[1:])
            while len(self.rows[sheet]) < row_number:
                self.rows[sheet].append([])
            row = self.rows[sheet][row_number - 1]
            self.rows[sheet][row_number - 1] = [*row[:9], *([""] * max(0, 9 - len(row))), values[0][0]]
        else:
            row_number = int(cells.split(":", 1)[0][1:])
            self.rows[sheet][row_number - 1] = list(values[0])
            self.events.append(("update", sheet, row_number))
        return range_name

    def _append_values(self, range_name, values):
        sheet = range_name.replace("'", "").split("!", 1)[0]
        self.rows[sheet].append(list(values[0]))
        row_number = len(self.rows[sheet])
        self.events.append(("append", sheet, row_number))
        if self.fail_after_append:
            self.fail_after_append = False
            raise TimeoutError("response lost after append")
        return f"{sheet}!A{row_number}:J{row_number}"

    def _hide_record_id_column(self, sheet_id):
        self.events.append(("hide", sheet_id))

    def _delete_sheet_row(self, sheet_name, row_number, history_id):
        assert self.rows[sheet_name][row_number - 1][9] == history_id
        self.events.append(("delete", sheet_name, row_number))
        self.rows[sheet_name].pop(row_number - 1)


class GoogleSheetsHistoryServiceTest(unittest.TestCase):
    def projection(self, history_id="id-1", sheet="2026"):
        return SheetHistoryProjection(history_id, sheet, ["value"] * 9)

    def test_append_adds_record_id_header_and_hides_column(self):
        service = MemoryHistorySheets()

        result = service.upsert_history_row(self.projection())

        self.assertEqual(2, result.row_number)
        self.assertEqual("RecordId", service.rows["2026"][0][9])
        self.assertEqual("id-1", service.rows["2026"][1][9])
        self.assertIn(("hide", 2), service.events)

    def test_stale_hint_finds_moved_row_by_record_id(self):
        rows = {
            "2026": [["h"] * 9 + ["RecordId"], ["wrong"] * 9 + ["other"], ["old"] * 9 + ["id-1"]]
        }
        service = MemoryHistorySheets(rows)

        result = service.upsert_history_row(self.projection(), "2026", 2)

        self.assertEqual(3, result.row_number)
        self.assertEqual("other", service.rows["2026"][1][9])
        self.assertEqual(["value"] * 9 + ["id-1"], service.rows["2026"][2])

    def test_duplicate_record_ids_block_mutation(self):
        rows = {
            "2026": [["h"] * 9 + ["RecordId"], ["a"] * 9 + ["id-1"], ["b"] * 9 + ["id-1"]]
        }
        service = MemoryHistorySheets(rows)

        with self.assertRaises(DuplicateRecordIdError):
            service.upsert_history_row(self.projection())

        self.assertNotIn(("update", "2026", 2), service.events)

    def test_cross_year_move_writes_target_before_deleting_source(self):
        rows = {
            "2025": [["h"] * 9 + ["RecordId"], ["old"] * 9 + ["id-1"]],
            "2026": [["h"] * 9 + ["RecordId"]],
        }
        service = MemoryHistorySheets(rows)

        result = service.upsert_history_row(self.projection(), "2025", 2)

        self.assertEqual("2026", result.sheet_name)
        self.assertLess(
            service.events.index(("append", "2026", 2)),
            service.events.index(("delete", "2025", 2)),
        )

    def test_delete_does_not_trust_stale_hint(self):
        rows = {
            "2026": [["h"] * 9 + ["RecordId"], ["a"] * 9 + ["other"], ["b"] * 9 + ["id-1"]]
        }
        service = MemoryHistorySheets(rows)

        self.assertTrue(service.delete_history_row("id-1", "2026", 2))

        self.assertEqual("other", service.rows["2026"][1][9])
        self.assertEqual(2, len(service.rows["2026"]))

    def test_retry_after_lost_append_response_does_not_duplicate(self):
        service = MemoryHistorySheets()
        service.fail_after_append = True

        with self.assertRaises(TimeoutError):
            service.upsert_history_row(self.projection())
        result = service.upsert_history_row(self.projection())

        self.assertEqual(2, result.row_number)
        self.assertEqual(1, sum(row[9] == "id-1" for row in service.rows["2026"][1:]))

    def test_record_id_backfill_is_idempotent_and_rejects_conflict(self):
        service = MemoryHistorySheets({"2026": [["h"] * 9, ["value"] * 9]})

        service.backfill_record_id("2026", 2, "id-1")
        service.backfill_record_id("2026", 2, "id-1")

        self.assertEqual("id-1", service.rows["2026"][1][9])
        with self.assertRaisesRegex(ValueError, "conflict"):
            service.backfill_record_id("2026", 2, "id-2")


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


class _FakeJsonResponse(_FakeResponse):
    def __init__(self, payload) -> None:
        self.payload = payload

    def read(self) -> bytes:
        import json

        return json.dumps(self.payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
