from __future__ import annotations

from dataclasses import asdict, dataclass

from backend.app.models.domain import DoubanMatchInput
from backend.app.services.matching_service import (
    CachedDoubanSearchAdapter,
    DoubanHttpSearchAdapter,
    DoubanSearchAdapter,
    FileDoubanSearchCache,
)


@dataclass(frozen=True)
class MovieSearchCandidate:
    subject_id: str
    title: str
    year: int | None
    director: str | None
    url: str | None


class MovieSearchService:
    def __init__(self, adapter: DoubanSearchAdapter, limit: int = 5) -> None:
        self.adapter = adapter
        self.limit = limit

    def search(self, query: str) -> list[MovieSearchCandidate]:
        normalized_query = " ".join(query.split())
        if not normalized_query:
            raise ValueError("query is required")

        match_input = DoubanMatchInput(
            source_raw_id="api-search",
            source_file="api",
            source_row_number=0,
            title=normalized_query,
            strategy="metadata",
        )
        results = self.adapter.search(match_input)[: self.limit]
        return [
            MovieSearchCandidate(
                subject_id=result.subject_id,
                title=result.title,
                year=result.year,
                director=result.director,
                url=result.url,
            )
            for result in results
        ]

    def to_response(self, query: str, candidates: list[MovieSearchCandidate]) -> dict:
        return {
            "query": query,
            "items": [asdict(candidate) for candidate in candidates],
        }


def create_movie_search_service() -> MovieSearchService:
    return MovieSearchService(
        CachedDoubanSearchAdapter(
            DoubanHttpSearchAdapter(),
            FileDoubanSearchCache(),
        )
    )
