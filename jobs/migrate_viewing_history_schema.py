from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from backend.app.config import resolve_postgres_dsn
from backend.app.db.postgres_repository import PostgresViewingHistoryRepository


def snapshot(repository: PostgresViewingHistoryRepository) -> dict:
    columns = repository.connection.execute(
        """SELECT column_name, data_type, is_nullable, column_default
           FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'viewing_history'
           ORDER BY ordinal_position"""
    ).fetchall()
    indexes = repository.connection.execute(
        """SELECT indexname, indexdef FROM pg_indexes
           WHERE schemaname = 'public' AND tablename = 'viewing_history' ORDER BY indexname"""
    ).fetchall()
    return {
        "columns": [dict(row) for row in columns],
        "indexes": [dict(row) for row in indexes],
        "history_count": int(
            repository.connection.execute("SELECT COUNT(*) AS count FROM viewing_history").fetchone()["count"]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit or apply the idempotent viewing-history schema migration.")
    parser.add_argument("mode", choices=("audit", "apply"))
    parser.add_argument("--config-path", default=".env")
    parser.add_argument("--dsn")
    parser.add_argument("--output")
    args = parser.parse_args()

    with PostgresViewingHistoryRepository(resolve_postgres_dsn(args.dsn, args.config_path)) as repository:
        before = snapshot(repository)
        if args.mode == "apply":
            repository.initialize_schema()
        after = snapshot(repository)
        outbox_exists = repository.connection.execute(
            "SELECT to_regclass('public.sheet_sync_outbox') IS NOT NULL AS exists"
        ).fetchone()["exists"]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "before": before,
        "after": after,
        "sheet_sync_outbox_exists": bool(outbox_exists),
    }
    output = Path(args.output) if args.output else Path("data/audits") / f"viewing-history-schema-{args.mode}-{datetime.now():%Y%m%d-%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "history_count": after["history_count"], "outbox": bool(outbox_exists)}, indent=2))


if __name__ == "__main__":
    main()
