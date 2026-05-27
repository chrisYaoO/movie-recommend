from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
from typing import Literal

from backend.app.db.repository import ViewingHistoryRepository
from backend.app.db.sqlite_repository import SQLiteViewingHistoryRepository
from backend.app.services.metadata_service import (
    DEFAULT_CHROME_BINARY_PATH,
    DoubanDetailAdapter,
    DoubanSeleniumDetailAdapter,
)

EnrichmentStatus = Literal["existing", "fetched", "failed"]


@dataclass(frozen=True)
class DoubanEnrichmentItemResult:
    subject_id: str
    status: EnrichmentStatus
    title: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class DoubanEnrichmentRunResult:
    items: tuple[DoubanEnrichmentItemResult, ...]

    @property
    def fetched_count(self) -> int:
        return self._count("fetched")

    @property
    def existing_count(self) -> int:
        return self._count("existing")

    @property
    def failed_count(self) -> int:
        return self._count("failed")

    def _count(self, status: EnrichmentStatus) -> int:
        return sum(1 for item in self.items if item.status == status)


def enrich_douban_subjects(
    subject_ids: list[str],
    adapter: DoubanDetailAdapter,
    repository: ViewingHistoryRepository,
) -> DoubanEnrichmentRunResult:
    results: list[DoubanEnrichmentItemResult] = []
    for subject_id in subject_ids:
        normalized_subject_id = _normalize_subject_id(subject_id)
        if not normalized_subject_id:
            continue

        existing = repository.find_movie_by_subject_id(normalized_subject_id)
        if existing is not None:
            results.append(
                DoubanEnrichmentItemResult(
                    subject_id=existing.douban_subject_id,
                    status="existing",
                    title=existing.title,
                )
            )
            continue

        try:
            detail = adapter.fetch(normalized_subject_id)
            movie = repository.upsert_movie_detail(detail)
            results.append(
                DoubanEnrichmentItemResult(
                    subject_id=movie.douban_subject_id,
                    status="fetched",
                    title=movie.title,
                )
            )
        except Exception as exc:
            results.append(
                DoubanEnrichmentItemResult(
                    subject_id=normalized_subject_id,
                    status="failed",
                    error=str(exc),
                )
            )

    return DoubanEnrichmentRunResult(items=tuple(results))


def build_default_adapter(
    timeout_seconds: float,
    delay_seconds: float,
    chrome_binary_path: str | None = None,
) -> DoubanSeleniumDetailAdapter:
    return DoubanSeleniumDetailAdapter(
        timeout_seconds=timeout_seconds,
        delay_seconds=delay_seconds,
        chrome_binary_path=chrome_binary_path,
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Enrich Douban movie details into the local movies table.")
    parser.add_argument("subject_ids", nargs="+", help="Douban subject IDs to enrich.")
    parser.add_argument("--db-path", default="data/movies.sqlite3")
    parser.add_argument("--chrome-binary-path", default=DEFAULT_CHROME_BINARY_PATH)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    args = parser.parse_args()

    repository = SQLiteViewingHistoryRepository(Path(args.db_path))
    repository.initialize_schema()
    adapter = build_default_adapter(
        timeout_seconds=args.timeout_seconds,
        delay_seconds=args.delay_seconds,
        chrome_binary_path=args.chrome_binary_path,
    )
    try:
        result = enrich_douban_subjects(args.subject_ids, adapter, repository)
    finally:
        adapter.close()
        repository.close()

    print(
        json.dumps(
            {
                "fetched": result.fetched_count,
                "existing": result.existing_count,
                "failed": result.failed_count,
                "items": [asdict(item) for item in result.items],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

def _normalize_subject_id(subject_id: str) -> str:
    return subject_id.strip()


if __name__ == "__main__":
    main()
