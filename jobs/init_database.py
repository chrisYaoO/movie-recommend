from __future__ import annotations

import argparse
import os

from backend.app.config import load_local_env, resolve_postgres_dsn
from backend.app.db.postgres_repository import PostgresViewingHistoryRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the PostgreSQL schema used by the application.")
    parser.add_argument("--config-path", default=".env")
    parser.add_argument("--dsn")
    parser.add_argument(
        "--if-postgres",
        action="store_true",
        help="Skip initialization unless MOVIES_RECOMMENDATION_BACKEND=postgres (for launch scripts).",
    )
    args = parser.parse_args()

    load_local_env(args.config_path)
    if args.if_postgres and os.getenv("MOVIES_RECOMMENDATION_BACKEND", "memory").lower() != "postgres":
        return

    with PostgresViewingHistoryRepository(resolve_postgres_dsn(args.dsn, args.config_path)) as repository:
        repository.initialize_schema()
    print("PostgreSQL schema initialized.")


if __name__ == "__main__":
    main()
