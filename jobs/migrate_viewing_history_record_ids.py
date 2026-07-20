from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

from backend.app.config import resolve_postgres_dsn, resolve_service_account_file, resolve_spreadsheet_id
from backend.app.services.google_sheets_service import GoogleSheetsHistoryService
from jobs.audit_viewing_history_sheet_sync import build_report
from jobs.sync_google_sheets_history import GoogleSheetsValuesClient


BLOCKING_STATUSES = ("local_only", "content_conflict", "ambiguous", "duplicate_record_id")


def apply_record_id_migration(
    report: dict[str, Any],
    connection: Any,
    sheets: GoogleSheetsHistoryService,
    completed_ids: set[str] | None = None,
    on_complete: Callable[[set[str]], None] | None = None,
) -> dict[str, Any]:
    blocking = {name: report["relationship_counts"].get(name, 0) for name in BLOCKING_STATUSES}
    if any(blocking.values()):
        raise ValueError(f"audit contains unresolved relationships: {blocking}")

    completed = completed_ids if completed_ids is not None else set()
    decisions: list[dict[str, Any]] = []
    for relationship in report.get("relationships", []):
        if relationship["status"] not in {"matched", "relinked"}:
            continue
        local = relationship["local"]
        sheet = relationship["sheet_rows"][0]
        history_id = str(local["id"])
        if history_id in completed:
            decisions.append({"history_id": history_id, "action": "already_completed"})
            continue

        sheets.backfill_record_id(str(sheet["sheet_name"]), int(sheet["row_number"]), history_id)
        if relationship["status"] == "relinked":
            with connection.transaction():
                connection.execute(
                    """UPDATE viewing_history
                       SET source_sheet_name = %s, source_row_number = %s, updated_at = %s
                       WHERE id = %s""",
                    (
                        str(sheet["sheet_name"]),
                        int(sheet["row_number"]),
                        datetime.now(timezone.utc),
                        history_id,
                    ),
                )
        completed.add(history_id)
        if on_complete:
            on_complete(completed)
        decisions.append(
            {
                "history_id": history_id,
                "status": relationship["status"],
                "action": "record_id_backfilled",
                "sheet_name": sheet["sheet_name"],
                "row_number": sheet["row_number"],
            }
        )
    return {
        "eligible_count": sum(
            1 for item in report.get("relationships", []) if item["status"] in {"matched", "relinked"}
        ),
        "completed_count": len(completed),
        "decisions": decisions,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit or apply viewing-history RecordId migration.")
    parser.add_argument("mode", choices=("audit", "apply"))
    parser.add_argument("--config-path", default=".env")
    parser.add_argument("--dsn")
    parser.add_argument("--sheet", action="append")
    parser.add_argument("--audit-report", help="Reviewed audit JSON; required for apply mode.")
    parser.add_argument("--output")
    parser.add_argument("--state-path", default="data/cache/viewing-history-record-id-migration.json")
    args = parser.parse_args()

    spreadsheet_id = resolve_spreadsheet_id(args.config_path)
    service_account_file = resolve_service_account_file(args.config_path)
    if not spreadsheet_id or not service_account_file:
        parser.error("Google Sheets spreadsheet ID and service account are required")

    import psycopg
    from psycopg.rows import dict_row

    if args.mode == "audit":
        client = GoogleSheetsValuesClient(spreadsheet_id, service_account_file=service_account_file)
        with psycopg.connect(resolve_postgres_dsn(args.dsn, args.config_path), row_factory=dict_row) as connection:
            report = build_report(connection, client, args.sheet or client.sheet_names())
        output = Path(args.output) if args.output else Path("data/audits") / f"viewing-history-record-id-audit-{datetime.now():%Y%m%d-%H%M%S}.json"
        _write_json(output, report)
        print(json.dumps({"report": str(output), "counts": report["relationship_counts"]}, indent=2))
        return

    if not args.audit_report:
        parser.error("--audit-report is required for apply mode")
    audit_path = Path(args.audit_report)
    report = json.loads(audit_path.read_text(encoding="utf-8"))
    if "relationships" not in report:
        parser.error("audit report predates migration support; run audit mode again")
    state_path = Path(args.state_path)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"completed_ids": []}
    completed = set(state.get("completed_ids", []))
    sheets = GoogleSheetsHistoryService(spreadsheet_id, service_account_file)
    with psycopg.connect(resolve_postgres_dsn(args.dsn, args.config_path), row_factory=dict_row) as connection:
        result = apply_record_id_migration(
            report,
            connection,
            sheets,
            completed,
            lambda ids: _write_json(
                state_path,
                {"audit_report": str(audit_path), "completed_ids": sorted(ids)},
            ),
        )
    output = Path(args.output) if args.output else Path("data/audits") / f"viewing-history-record-id-apply-{datetime.now():%Y%m%d-%H%M%S}.json"
    application_report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply",
        "source_audit": str(audit_path),
        **result,
    }
    _write_json(output, application_report)
    print(json.dumps({"report": str(output), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
