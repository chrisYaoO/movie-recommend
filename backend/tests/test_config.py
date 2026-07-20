import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.config import load_local_env, resolve_service_account_file, resolve_spreadsheet_id


class ConfigTest(unittest.TestCase):
    def test_load_local_env_sets_missing_values_without_overriding_process_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "MOVIES_RECOMMENDATION_BACKEND=postgres",
                        "MOVIES_POSTGRES_DSN=postgresql://from-file",
                        "IGNORED_WITHOUT_EQUALS",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"MOVIES_POSTGRES_DSN": "postgresql://from-env"}, clear=True):
                loaded = load_local_env(env_path)

                self.assertEqual("postgres", os.environ["MOVIES_RECOMMENDATION_BACKEND"])
                self.assertEqual("postgresql://from-env", os.environ["MOVIES_POSTGRES_DSN"])
                self.assertEqual({"MOVIES_RECOMMENDATION_BACKEND": "postgres"}, loaded)

    def test_google_sheets_environment_overrides_support_isolated_runtime(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE": "/private/tmp/missing-test-credentials.json",
                "GOOGLE_SHEETS_SPREADSHEET_ID": "isolated-test-spreadsheet-id-12345",
            },
        ):
            self.assertEqual(
                "/private/tmp/missing-test-credentials.json",
                resolve_service_account_file("missing.env"),
            )
            self.assertEqual(
                "isolated-test-spreadsheet-id-12345",
                resolve_spreadsheet_id("missing.env"),
            )


if __name__ == "__main__":
    unittest.main()


