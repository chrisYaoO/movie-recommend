from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from backend.app.config import resolve_postgres_dsn, resolve_service_account_file, resolve_spreadsheet_id
from backend.app.services.import_service import COLUMN_ALIASES, RAW_HASH_COLUMNS, _normalize_cell, stable_row_hash
from jobs.sync_google_sheets_history import GoogleSheetsValuesClient, _range_name


STATUSES = (
    "matched",
    "relinked",
    "local_only",
    "sheet_only",
    "content_conflict",
    "ambiguous",
    "duplicate_record_id",
)


def classify(local_rows: list[dict[str, Any]], sheet_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    assigned_local: set[str] = set()
    assigned_sheet: set[tuple[str, int]] = set()
    local_by_id = {str(row["id"]): row for row in local_rows}
    sheet_by_locator = {_locator(row): row for row in sheet_rows}

    record_id_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sheet_rows:
        if row["record_id"]:
            record_id_rows[row["record_id"]].append(row)

    for record_id, rows in record_id_rows.items():
        if len(rows) < 2:
            continue
        local = local_by_id.get(record_id)
        results.append(_result("duplicate_record_id", local, rows, "RecordId appears in multiple Sheet rows"))
        if local:
            assigned_local.add(record_id)
        assigned_sheet.update(_locator(row) for row in rows)

    for record_id, rows in record_id_rows.items():
        if len(rows) != 1 or record_id not in local_by_id:
            continue
        local, sheet = local_by_id[record_id], rows[0]
        if record_id in assigned_local or _locator(sheet) in assigned_sheet:
            continue
        if _checksum_matches(local["source_row_checksum"], sheet):
            status = "matched" if _locator(local) == _locator(sheet) else "relinked"
            reason = "RecordId and checksum agree"
        else:
            status, reason = "content_conflict", "RecordId agrees but A:I checksum differs"
        results.append(_result(status, local, [sheet], reason))
        assigned_local.add(record_id)
        assigned_sheet.add(_locator(sheet))

    for local in local_rows:
        local_id = str(local["id"])
        sheet = sheet_by_locator.get(_locator(local))
        if (
            local_id not in assigned_local
            and sheet
            and _locator(sheet) not in assigned_sheet
            and not sheet["record_id"]
            and _checksum_matches(local["source_row_checksum"], sheet)
        ):
            results.append(_result("matched", local, [sheet], "locator and checksum agree"))
            assigned_local.add(local_id)
            assigned_sheet.add(_locator(sheet))

    unresolved_locals = [row for row in local_rows if str(row["id"]) not in assigned_local]
    unresolved_sheets = [row for row in sheet_rows if _locator(row) not in assigned_sheet and not row["record_id"]]
    locals_by_checksum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sheets_by_checksum: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in unresolved_locals:
        locals_by_checksum[row["source_row_checksum"]].append(row)
    for row in unresolved_sheets:
        for checksum in row.get("checksum_variants", [row["checksum"]]):
            sheets_by_checksum[checksum].append(row)

    for checksum, locals_with_checksum in locals_by_checksum.items():
        sheets_with_checksum = sheets_by_checksum.get(checksum, [])
        if len(locals_with_checksum) == len(sheets_with_checksum) == 1:
            local, sheet = locals_with_checksum[0], sheets_with_checksum[0]
            results.append(_result("relinked", local, [sheet], "checksum uniquely matches a different Sheet row"))
            assigned_local.add(str(local["id"]))
            assigned_sheet.add(_locator(sheet))

    for checksum, locals_with_checksum in locals_by_checksum.items():
        unresolved_checksum_locals = [row for row in locals_with_checksum if str(row["id"]) not in assigned_local]
        unresolved_checksum_sheets = [
            row for row in sheets_by_checksum.get(checksum, []) if _locator(row) not in assigned_sheet
        ]
        if not unresolved_checksum_sheets or (
            len(unresolved_checksum_locals) == len(unresolved_checksum_sheets) == 1
        ):
            continue
        for local in unresolved_checksum_locals:
            results.append(
                _result(
                    "ambiguous",
                    local,
                    unresolved_checksum_sheets,
                    "checksum does not produce a one-to-one match",
                )
            )
            assigned_local.add(str(local["id"]))
        assigned_sheet.update(_locator(row) for row in unresolved_checksum_sheets)

    for local in local_rows:
        local_id = str(local["id"])
        if local_id in assigned_local:
            continue
        if (locator_row := sheet_by_locator.get(_locator(local))) is not None:
            status, rows, reason = "content_conflict", [locator_row], "locator exists but A:I checksum differs"
            assigned_sheet.add(_locator(locator_row))
        else:
            status, rows, reason = "local_only", [], "no Sheet row matches locator or checksum"
        results.append(_result(status, local, rows, reason))
        assigned_local.add(local_id)

    for sheet in sheet_rows:
        if _locator(sheet) not in assigned_sheet:
            results.append(_result("sheet_only", None, [sheet], "no local row matches RecordId, locator, or checksum"))

    return results


def build_report(connection: Any, client: GoogleSheetsValuesClient, sheet_names: list[str]) -> dict[str, Any]:
    connection.execute("SET TRANSACTION READ ONLY")
    columns = connection.execute(
        """SELECT column_name, data_type, is_nullable, column_default
           FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'viewing_history'
           ORDER BY ordinal_position"""
    ).fetchall()
    indexes = connection.execute(
        """SELECT indexname, indexdef FROM pg_indexes
           WHERE schemaname = 'public' AND tablename = 'viewing_history' ORDER BY indexname"""
    ).fetchall()
    has_deleted_at = any(str(row["column_name"]) == "deleted_at" for row in columns)
    local_rows = connection.execute(
        f"""SELECT vh.id, vh.source_sheet_name, vh.source_row_number, vh.source_row_checksum,
                  vh.douban_subject_id, COALESCE(m.title, '') AS title
           FROM viewing_history vh LEFT JOIN movies m ON m.id = vh.movie_id
           {"WHERE vh.deleted_at IS NULL" if has_deleted_at else ""}
           ORDER BY vh.source_sheet_name, vh.source_row_number"""
    ).fetchall()
    missing = connection.execute(
        """SELECT COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE id IS NULL) AS missing_uuid,
                  COUNT(*) FILTER (WHERE source_row_checksum IS NULL OR btrim(source_row_checksum) = '') AS missing_checksum,
                  COUNT(*) FILTER (WHERE source_sheet_name IS NULL OR btrim(source_sheet_name) = '') AS missing_sheet_name,
                  COUNT(*) FILTER (WHERE source_row_number IS NULL) AS missing_row_number,
                  COUNT(*) FILTER (WHERE douban_subject_id IS NULL OR btrim(douban_subject_id) = '') AS missing_douban_subject_id
           FROM viewing_history"""
    ).fetchone()

    sheet_rows: list[dict[str, Any]] = []
    sheets: list[dict[str, Any]] = []
    for sheet_name in sheet_names:
        values = client.values(_range_name(sheet_name, "A:Z"))
        header = [str(value).strip() if value is not None else "" for value in (values[0] if values else [])]
        rows = []
        for row_number, row_values in enumerate(values[1:], start=2):
            if not any(_normalize_cell(value) for value in row_values):
                continue
            mapped = _mapped_values(header, row_values)
            row = {
                "sheet_name": sheet_name,
                "row_number": row_number,
                "checksum": stable_row_hash(mapped),
                "checksum_variants": _checksum_variants(mapped, sheet_name),
                "record_id": _valid_record_id(_header_value(header, row_values, "RecordId")),
                "record_id_raw": _normalize_cell(_header_value(header, row_values, "RecordId")),
                "title": mapped.get("Name") or "",
            }
            rows.append(row)
            sheet_rows.append(row)
        used_columns = max((index + 1 for row in values[1:] for index, value in enumerate(row) if _normalize_cell(value)), default=0)
        sheets.append(
            {
                "sheet_name": sheet_name,
                "header": header,
                "data_row_count": len(rows),
                "last_used_column": _column_name(used_columns),
                "extra_columns_after_I": [
                    {"column": _column_name(index + 1), "header": name}
                    for index, name in enumerate(header)
                    if index >= 9 and name
                ],
            }
        )

    results = classify([dict(row) for row in local_rows], sheet_rows)
    counts = Counter(result["status"] for result in results)
    local_counts = Counter(str(row["source_sheet_name"]) for row in local_rows)
    invalid_record_ids = [
        {"sheet_name": row["sheet_name"], "row_number": row["row_number"], "value": row["record_id_raw"]}
        for row in sheet_rows
        if row["record_id_raw"] and not row["record_id"]
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only",
        "database": {
            "schema": [dict(row) for row in columns],
            "indexes": [dict(row) for row in indexes],
            "counts_by_source_sheet_name": dict(sorted(local_counts.items())),
            "missing_fields": dict(missing),
        },
        "google_sheets": {"sheets": sheets, "invalid_record_ids": invalid_record_ids},
        "relationship_counts": {status: counts[status] for status in STATUSES},
        "relationships": results,
        "non_matched": [result for result in results if result["status"] != "matched"],
    }


def _mapped_values(header: list[str], row_values: list[Any]) -> dict[str, str | None]:
    values: dict[str, str | None] = {column: None for column in RAW_HASH_COLUMNS}
    for index, name in enumerate(header):
        canonical = COLUMN_ALIASES.get(name, name)
        if canonical in values and values[canonical] is None:
            values[canonical] = _normalize_cell(row_values[index] if index < len(row_values) else None)
    for index, canonical in enumerate(RAW_HASH_COLUMNS):
        if values[canonical] is None and (index >= len(header) or not header[index]):
            values[canonical] = _normalize_cell(row_values[index] if index < len(row_values) else None)
    return values


def _checksum_variants(values: dict[str, str | None], sheet_name: str) -> list[str]:
    variants = [values]
    canonical_date = _canonical_date(values.get("Date"), sheet_name)
    canonical_rating = _canonical_rating(values.get("Rating"))
    if canonical_date != values.get("Date"):
        variants.append({**values, "Date": canonical_date})
    if canonical_rating != values.get("Rating"):
        variants += [{**variant, "Rating": canonical_rating} for variant in list(variants)]
    return list(dict.fromkeys(stable_row_hash(variant) for variant in variants))


def _canonical_date(value: str | None, sheet_name: str) -> str | None:
    if not value:
        return None
    formats = (("%Y-%m-%d", False), ("%m/%d/%Y", False), ("%m/%d", True))
    for format_text, needs_year in formats:
        try:
            parsed = datetime.strptime(value, format_text)
            if needs_year:
                parsed = parsed.replace(year=int(sheet_name))
            return parsed.date().isoformat()
        except (ValueError, TypeError):
            continue
    return value


def _canonical_rating(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(float(value))
    except ValueError:
        return value


def _checksum_matches(checksum: str, sheet: dict[str, Any]) -> bool:
    return checksum in sheet.get("checksum_variants", [sheet["checksum"]])


def _header_value(header: list[str], row_values: list[Any], name: str) -> Any:
    try:
        index = header.index(name)
    except ValueError:
        return None
    return row_values[index] if index < len(row_values) else None


def _valid_record_id(value: Any) -> str | None:
    normalized = _normalize_cell(value)
    if not normalized:
        return None
    try:
        return str(UUID(normalized))
    except ValueError:
        return None


def _locator(row: dict[str, Any]) -> tuple[str, int]:
    return str(row["source_sheet_name"] if "source_sheet_name" in row else row["sheet_name"]), int(row["source_row_number"] if "source_row_number" in row else row["row_number"])


def _result(status: str, local: dict[str, Any] | None, sheets: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "local": None if local is None else {
            "id": str(local["id"]),
            "sheet_name": str(local["source_sheet_name"]),
            "row_number": int(local["source_row_number"]),
            "checksum": str(local["source_row_checksum"]),
            "douban_subject_id": str(local["douban_subject_id"]),
            "title": str(local.get("title") or ""),
        },
        "sheet_rows": [
            {
                "sheet_name": row["sheet_name"],
                "row_number": row["row_number"],
                "checksum": row["checksum"],
                "record_id": row["record_id_raw"],
                "title": row["title"],
            }
            for row in sheets
        ],
    }


def _column_name(number: int) -> str | None:
    if number < 1:
        return None
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only audit of PostgreSQL viewing history against Google Sheets.")
    parser.add_argument("--config-path", default=".env")
    parser.add_argument("--dsn")
    parser.add_argument("--sheet", action="append", help="Sheet tab to audit; defaults to every tab.")
    parser.add_argument("--output", help="JSON report path; defaults to a timestamped file under data/audits.")
    args = parser.parse_args()

    spreadsheet_id = resolve_spreadsheet_id(args.config_path)
    service_account_file = resolve_service_account_file(args.config_path)
    if not spreadsheet_id or not service_account_file:
        parser.error("Google Sheets spreadsheet ID and service account are required")

    import psycopg
    from psycopg.rows import dict_row

    client = GoogleSheetsValuesClient(spreadsheet_id, service_account_file=service_account_file)
    sheet_names = args.sheet or client.sheet_names()
    with psycopg.connect(resolve_postgres_dsn(args.dsn, args.config_path), row_factory=dict_row) as connection:
        report = build_report(connection, client, sheet_names)

    output = Path(args.output) if args.output else Path("data/audits") / f"viewing-history-sheet-sync-{datetime.now():%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "counts": report["relationship_counts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
