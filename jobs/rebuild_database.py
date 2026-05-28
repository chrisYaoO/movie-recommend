from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

from jobs.import_auto_matched_history import resolve_postgres_dsn


COUNT_TABLES = [
    "movies",
    "viewing_history",
    "candidate_subject_queue",
    "candidate_pool",
    "history_recommendation_discovery",
    "recommendation_sessions",
    "recommendation_items",
    "feedback",
    "wishlist",
]

CLEAR_TABLES = [
    "feedback",
    "wishlist",
    "recommendation_items",
    "recommendation_sessions",
    "history_recommendation_discovery",
    "viewing_history",
    "candidate_pool",
    "movies",
]

MOVIE_COLUMNS_TO_DROP = ["display_title", "original_title"]


@dataclass(frozen=True)
class CandidatePoolQueueBackup:
    douban_subject_id: str
    source_type: str
    source_ref: str
    source_label: str | None


@dataclass(frozen=True)
class RebuildInspection:
    row_counts: dict[str, int]
    movie_columns_to_drop: list[str]
    candidate_pool_rows_to_preserve_in_queue: list[CandidatePoolQueueBackup]
    clear_tables: list[str]
    preserve_tables: list[str]


@dataclass(frozen=True)
class ClearSummary:
    dry_run: bool
    before: RebuildInspection
    candidate_pool_rows_preserved_in_queue: int
    cleared_tables: list[str]
    dropped_movie_columns: list[str]
    after_row_counts: dict[str, int] | None


def inspect_rebuild_state(conn) -> RebuildInspection:
    row_counts = {table: _count_rows(conn, table) for table in COUNT_TABLES}
    existing_movie_columns = _existing_columns(conn, "movies")
    pool_rows = conn.execute(
        """
        SELECT
            m.douban_subject_id,
            cp.source_type,
            cp.source_ref,
            m.title AS source_label
        FROM candidate_pool cp
        JOIN movies m ON m.id = cp.movie_id
        ORDER BY cp.created_at, cp.id
        """
    ).fetchall()
    return RebuildInspection(
        row_counts=row_counts,
        movie_columns_to_drop=[column for column in MOVIE_COLUMNS_TO_DROP if column in existing_movie_columns],
        candidate_pool_rows_to_preserve_in_queue=[
            CandidatePoolQueueBackup(
                douban_subject_id=str(row["douban_subject_id"]),
                source_type=str(row["source_type"]),
                source_ref=str(row["source_ref"]),
                source_label=str(row["source_label"]) if row["source_label"] is not None else None,
            )
            for row in pool_rows
        ],
        clear_tables=CLEAR_TABLES,
        preserve_tables=["candidate_subject_queue"],
    )


def clear_non_queue_tables(conn, dry_run: bool) -> ClearSummary:
    before = inspect_rebuild_state(conn)
    if dry_run:
        return ClearSummary(
            dry_run=True,
            before=before,
            candidate_pool_rows_preserved_in_queue=len(before.candidate_pool_rows_to_preserve_in_queue),
            cleared_tables=[],
            dropped_movie_columns=[],
            after_row_counts=None,
        )

    with conn.transaction():
        for row in before.candidate_pool_rows_to_preserve_in_queue:
            conn.execute(
                """
                INSERT INTO candidate_subject_queue (
                    douban_subject_id,
                    source_type,
                    source_ref,
                    source_subject_id,
                    source_label,
                    status,
                    error,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, NULL, %s, 'pending', NULL, NOW(), NOW())
                ON CONFLICT(douban_subject_id) DO UPDATE SET
                    source_type = excluded.source_type,
                    source_ref = excluded.source_ref,
                    source_label = COALESCE(candidate_subject_queue.source_label, excluded.source_label),
                    updated_at = excluded.updated_at
                """,
                (row.douban_subject_id, row.source_type, row.source_ref, row.source_label),
            )

        conn.execute("TRUNCATE TABLE " + ", ".join(CLEAR_TABLES))
        for column in before.movie_columns_to_drop:
            conn.execute(f"ALTER TABLE movies DROP COLUMN IF EXISTS {column}")

    return ClearSummary(
        dry_run=False,
        before=before,
        candidate_pool_rows_preserved_in_queue=len(before.candidate_pool_rows_to_preserve_in_queue),
        cleared_tables=CLEAR_TABLES,
        dropped_movie_columns=before.movie_columns_to_drop,
        after_row_counts={table: _count_rows(conn, table) for table in COUNT_TABLES},
    )


def _count_rows(conn, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        """,
        (table,),
    ).fetchall()
    return {str(row["column_name"]) for row in rows}


def _connect(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row)


def _summary_to_json(summary: ClearSummary | RebuildInspection) -> str:
    return json.dumps(asdict(summary), ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and prepare the PostgreSQL database rebuild.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Print row counts and rebuild blockers.")
    inspect_parser.add_argument("--dsn", default=None)
    inspect_parser.add_argument("--config-path", default=".env")

    clear_parser = subparsers.add_parser(
        "clear-non-queue-tables",
        help="Preserve candidate_subject_queue, convert candidate_pool rows back to queue, then clear rebuild tables.",
    )
    clear_parser.add_argument("--dsn", default=None)
    clear_parser.add_argument("--config-path", default=".env")
    clear_parser.add_argument("--dry-run", action="store_true")
    clear_parser.add_argument(
        "--confirm-clear-non-queue-tables",
        action="store_true",
        help="Required unless --dry-run is supplied.",
    )

    args = parser.parse_args()
    dsn = resolve_postgres_dsn(args.dsn, args.config_path)
    with _connect(dsn) as conn:
        if args.command == "inspect":
            print(_summary_to_json(inspect_rebuild_state(conn)))
            return

        if not args.dry_run and not args.confirm_clear_non_queue_tables:
            parser.error("--confirm-clear-non-queue-tables is required without --dry-run")
        print(_summary_to_json(clear_non_queue_tables(conn, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
