import unittest

from backend.app.services.google_sheets_service import _row_number_from_updated_range


class GoogleSheetsServiceTest(unittest.TestCase):
    def test_parses_row_number_from_updated_range(self) -> None:
        self.assertEqual(27, _row_number_from_updated_range("2026!A27:I27"))
        self.assertEqual(27, _row_number_from_updated_range("'Movie Reviews'!A27:I27"))

    def test_rejects_updated_range_without_row_number(self) -> None:
        with self.assertRaisesRegex(ValueError, "updatedRange"):
            _row_number_from_updated_range("2026!A:I")


if __name__ == "__main__":
    unittest.main()
