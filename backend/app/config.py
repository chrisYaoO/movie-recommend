import json
import os
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVICE_ACCOUNT_FILE = ".secrets/google-sheets-service-account.json"
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def load_local_env(path: str | Path | None = None) -> dict[str, str]:
    env_path = Path(path) if path is not None else PROJECT_ROOT / ".env"
    loaded: dict[str, str] = {}
    for key, value in _config_values(env_path).items():
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def config_value(path: str | Path, key: str) -> str | None:
    return _config_values(Path(path)).get(key)


def resolve_postgres_dsn(dsn_arg: str | None, config_path: str | Path = ".env") -> str:
    env_dsn = os.getenv("MOVIES_POSTGRES_DSN")
    config_dsn = config_value(config_path, "MOVIES_POSTGRES_DSN")
    env_literals = {"$env:MOVIES_POSTGRES_DSN", "%MOVIES_POSTGRES_DSN%"}
    if dsn_arg and dsn_arg not in env_literals:
        return dsn_arg
    if env_dsn:
        return env_dsn
    if config_dsn:
        return config_dsn
    if dsn_arg in env_literals:
        raise ValueError(
            f"{dsn_arg} was passed literally; set MOVIES_POSTGRES_DSN, add it to {config_path}, or pass the actual DSN."
        )
    raise ValueError(f"PostgreSQL DSN is required. Pass --dsn, set MOVIES_POSTGRES_DSN, or add it to {config_path}.")


def resolve_service_account_file(config_path: str | Path) -> str | None:
    configured = os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE") or config_value(
        config_path, "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE"
    )
    if configured:
        return configured
    return DEFAULT_SERVICE_ACCOUNT_FILE if Path(DEFAULT_SERVICE_ACCOUNT_FILE).exists() else None


def resolve_spreadsheet_id(config_path: str | Path) -> str | None:
    environment_id = _extract_spreadsheet_id(os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID"))
    if environment_id:
        return environment_id
    service_account_file = resolve_service_account_file(config_path)
    if service_account_file and Path(service_account_file).exists():
        payload = json.loads(Path(service_account_file).read_text(encoding="utf-8"))
        for key in ("spreadsheet_id", "google_sheets_spreadsheet_id", "sheet_id"):
            extracted = _extract_spreadsheet_id(str(payload.get(key) or ""))
            if extracted:
                return extracted
    return _extract_spreadsheet_id(config_value(config_path, "GOOGLE_SHEETS_SPREADSHEET_ID"))


def _config_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _extract_spreadsheet_id(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"/spreadsheets/d/([^/?#]+)", value)
    if match:
        return match.group(1)
    return value if re.fullmatch(r"[A-Za-z0-9_-]{25,}", value) else None
