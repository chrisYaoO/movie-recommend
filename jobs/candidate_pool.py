from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import re
import sys
import time
from typing import Callable, Protocol
from urllib.request import Request, urlopen

from backend.app.db.postgres_repository import PostgresViewingHistoryRepository
from backend.app.db.repository import ViewingHistoryRepository
from backend.app.models.domain import DoubanMovieDetail
from backend.app.services.metadata_service import DoubanDetailAdapter, DoubanSeleniumDetailAdapter
from jobs.import_auto_matched_history import resolve_postgres_dsn

DOUBAN_TOP250_SOURCE = "douban_top250"
DOUBAN_RECOMMENDATION_SOURCE = "douban_recommendation"
QUEUE_STATUS_ENRICHED = "enriched"
QUEUE_STATUS_FAILED = "failed"


class DoubanPageDetailAdapter(Protocol):
    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        pass

    @property
    def last_page_source(self) -> str | None:
        pass


@dataclass(frozen=True)
class Top250DiscoverySummary:
    discovered_count: int
    inserted_count: int


@dataclass(frozen=True)
class CandidateQueueProcessSummary:
    attempted_count: int
    enriched_count: int
    existing_movie_count: int
    candidate_pool_inserted_count: int
    recommendation_discovered_count: int
    recommendation_inserted_count: int
    failed_count: int


class DoubanTop250Client:
    def __init__(
        self,
        base_url: str = "https://movie.douban.com/top250",
        timeout_seconds: float = 20.0,
        delay_seconds: float = 1.0,
        user_agent: str = "Mozilla/5.0",
    ) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.delay_seconds = delay_seconds
        self.user_agent = user_agent
        self.last_request_at = 0.0

    def fetch_page(self, start: int) -> str:
        self._throttle()
        request = Request(
            f"{self.base_url}?start={start}&filter=",
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            html = response.read().decode("utf-8", errors="replace")
        self.last_request_at = time.monotonic()
        return html

    def _throttle(self) -> None:
        if self.last_request_at <= 0:
            return
        remaining = self.delay_seconds - (time.monotonic() - self.last_request_at)
        if remaining > 0:
            time.sleep(remaining)


class SeleniumDetailPageAdapter:
    def __init__(self, inner: DoubanSeleniumDetailAdapter) -> None:
        self.inner = inner
        self._last_page_source: str | None = None

    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        detail = self.inner.fetch(subject_id)
        driver = getattr(self.inner, "driver", None)
        self._last_page_source = getattr(driver, "page_source", None)
        return detail

    @property
    def last_page_source(self) -> str | None:
        return self._last_page_source

    def close(self) -> None:
        self.inner.close()


def discover_top250_subjects(
    repository: ViewingHistoryRepository,
    client: DoubanTop250Client,
) -> Top250DiscoverySummary:
    subjects: list[str] = []
    for start in range(0, 250, 25):
        subjects.extend(parse_top250_subject_ids(client.fetch_page(start)))

    unique_subjects = list(dict.fromkeys(subjects))
    inserted_count = 0
    for index, subject_id in enumerate(unique_subjects[:250], start=1):
        if repository.upsert_candidate_subject(
            subject_id,
            source_type=DOUBAN_TOP250_SOURCE,
            source_ref=f"top{index}",
        ):
            inserted_count += 1

    return Top250DiscoverySummary(discovered_count=len(unique_subjects[:250]), inserted_count=inserted_count)


def process_candidate_queue(
    repository: ViewingHistoryRepository,
    detail_adapter: DoubanPageDetailAdapter,
    limit: int | None = None,
    recommendation_parser: Callable[[str, str], list[str]] | None = None,
) -> CandidateQueueProcessSummary:
    if recommendation_parser is None:
        recommendation_parser = parse_recommended_subject_ids

    attempted_count = 0
    enriched_count = 0
    existing_movie_count = 0
    candidate_pool_inserted_count = 0
    recommendation_discovered_count = 0
    recommendation_inserted_count = 0
    failed_count = 0

    for item in repository.find_pending_candidate_subjects(limit=limit):
        attempted_count += 1
        try:
            existing = repository.find_movie_by_subject_id(item.douban_subject_id)
            if existing is None:
                detail = detail_adapter.fetch(item.douban_subject_id)
                movie = repository.upsert_movie_detail(detail)
                enriched_count += 1
                page_source = detail_adapter.last_page_source
                source_label = detail.title
            else:
                movie = existing
                existing_movie_count += 1
                page_source = None
                source_label = existing.title

            if repository.upsert_candidate_pool_entry(movie.id, item.source_type, item.source_ref):
                candidate_pool_inserted_count += 1

            if item.source_type == DOUBAN_TOP250_SOURCE and page_source:
                recommended_ids = recommendation_parser(page_source, item.douban_subject_id)
                recommendation_discovered_count += len(recommended_ids)
                for recommended_id in recommended_ids:
                    if repository.upsert_candidate_subject(
                        recommended_id,
                        source_type=DOUBAN_RECOMMENDATION_SOURCE,
                        source_ref=f"recommended_from:{item.douban_subject_id}",
                        source_subject_id=item.douban_subject_id,
                        source_label=f"recommended from {source_label}",
                    ):
                        recommendation_inserted_count += 1

            repository.mark_candidate_subject_status(item.douban_subject_id, QUEUE_STATUS_ENRICHED)
        except Exception as exc:
            failed_count += 1
            repository.mark_candidate_subject_status(item.douban_subject_id, QUEUE_STATUS_FAILED, str(exc))

    return CandidateQueueProcessSummary(
        attempted_count=attempted_count,
        enriched_count=enriched_count,
        existing_movie_count=existing_movie_count,
        candidate_pool_inserted_count=candidate_pool_inserted_count,
        recommendation_discovered_count=recommendation_discovered_count,
        recommendation_inserted_count=recommendation_inserted_count,
        failed_count=failed_count,
    )


def parse_top250_subject_ids(html: str) -> list[str]:
    ids: list[str] = []
    for match in re.finditer(r"https://movie\.douban\.com/subject/(\d+)/", html):
        subject_id = match.group(1)
        if subject_id not in ids:
            ids.append(subject_id)
    return ids


def parse_recommended_subject_ids(html: str, current_subject_id: str) -> list[str]:
    start = html.find("喜欢这部电影的人也喜欢")
    if start < 0:
        return []
    section = html[start : start + 20000]
    ids: list[str] = []
    for match in re.finditer(r"https://movie\.douban\.com/subject/(\d+)/", section):
        subject_id = match.group(1)
        if subject_id != current_subject_id and subject_id not in ids:
            ids.append(subject_id)
    return ids


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Discover and enrich local candidate-pool movies.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    top250 = subparsers.add_parser("discover-top250", help="Queue Douban Top250 subject IDs.")
    top250.add_argument("--dsn", default=None)
    top250.add_argument("--config-path", default=".env")
    top250.add_argument("--timeout-seconds", type=float, default=20.0)
    top250.add_argument("--delay-seconds", type=float, default=1.0)

    process = subparsers.add_parser("process-queue", help="Enrich queued candidates and activate candidate_pool.")
    process.add_argument("--dsn", default=None)
    process.add_argument("--config-path", default=".env")
    process.add_argument("--chrome-binary-path", default=None)
    process.add_argument("--timeout-seconds", type=float, default=20.0)
    process.add_argument("--delay-seconds", type=float, default=1.0)
    process.add_argument("--limit", type=int, default=None)

    args = parser.parse_args()
    dsn = resolve_postgres_dsn(args.dsn, args.config_path)
    repository = PostgresViewingHistoryRepository(dsn)
    repository.initialize_schema()

    detail_adapter: DoubanDetailAdapter | None = None
    try:
        if args.command == "discover-top250":
            result = discover_top250_subjects(
                repository,
                DoubanTop250Client(timeout_seconds=args.timeout_seconds, delay_seconds=args.delay_seconds),
            )
        else:
            selenium = DoubanSeleniumDetailAdapter(
                timeout_seconds=args.timeout_seconds,
                delay_seconds=args.delay_seconds,
                chrome_binary_path=args.chrome_binary_path,
            )
            detail_adapter = selenium
            result = process_candidate_queue(
                repository,
                SeleniumDetailPageAdapter(selenium),
                limit=args.limit,
            )
    finally:
        if detail_adapter is not None and hasattr(detail_adapter, "close"):
            detail_adapter.close()
        repository.close()

    print(json.dumps({"summary": asdict(result)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
