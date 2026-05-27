from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
import hashlib
from html import unescape
import json
from pathlib import Path
import re
import time
from typing import Protocol
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

from backend.app.models.domain import (
    ConfirmedViewingHistoryInput,
    DoubanMatchCandidate,
    DoubanMatchInput,
    DoubanMatchStatus,
    DoubanSearchResult,
    ViewingHistoryCandidate,
)

SUBJECT_ID_STRATEGY = "subject_id"
METADATA_STRATEGY = "metadata"


@dataclass(frozen=True)
class MatchQueuePreview:
    inputs: list[DoubanMatchInput]

    @property
    def total_count(self) -> int:
        return len(self.inputs)

    @property
    def subject_id_count(self) -> int:
        return sum(1 for item in self.inputs if item.strategy == SUBJECT_ID_STRATEGY)

    @property
    def metadata_count(self) -> int:
        return sum(1 for item in self.inputs if item.strategy == METADATA_STRATEGY)


@dataclass(frozen=True)
class MatchRunResult:
    candidates: list[DoubanMatchCandidate]

    @property
    def total_count(self) -> int:
        return len(self.candidates)

    @property
    def auto_matched_count(self) -> int:
        return sum(1 for item in self.candidates if item.status == DoubanMatchStatus.AUTO_MATCHED)

    @property
    def needs_review_count(self) -> int:
        return sum(1 for item in self.candidates if item.status == DoubanMatchStatus.NEEDS_REVIEW)


@dataclass(frozen=True)
class SearchMatchScore:
    status: DoubanMatchStatus
    score: float
    reason: str


class DoubanSearchAdapter(Protocol):
    def search(self, match_input: DoubanMatchInput) -> list[DoubanSearchResult]:
        pass


class DoubanSearchCache(Protocol):
    def get(self, match_input: DoubanMatchInput) -> list[DoubanSearchResult] | None:
        pass

    def set(self, match_input: DoubanMatchInput, results: list[DoubanSearchResult]) -> None:
        pass


class FakeDoubanSearchAdapter:
    def __init__(self, results_by_title: dict[str, list[DoubanSearchResult]] | None = None) -> None:
        self.results_by_title = results_by_title or {}
        self.searches: list[DoubanMatchInput] = []

    def search(self, match_input: DoubanMatchInput) -> list[DoubanSearchResult]:
        self.searches.append(match_input)
        return self.results_by_title.get(match_input.title, [])


class DoubanHttpSearchAdapter:
    def __init__(
        self,
        base_url: str = "https://www.douban.com/search",
        timeout_seconds: float = 10.0,
        delay_seconds: float = 1.0,
        user_agent: str = "Mozilla/5.0",
    ) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.delay_seconds = delay_seconds
        self.user_agent = user_agent
        self.last_request_at = 0.0

    def search(self, match_input: DoubanMatchInput) -> list[DoubanSearchResult]:
        self._throttle()
        request = Request(
            self._build_search_url(match_input),
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            html = response.read().decode("utf-8", errors="replace")
        self.last_request_at = time.monotonic()
        return parse_douban_search_results(html)

    def _build_search_url(self, match_input: DoubanMatchInput) -> str:
        return f"{self.base_url}?{urlencode({'cat': '1002', 'q': _search_query(match_input)})}"

    def _throttle(self) -> None:
        if self.last_request_at <= 0:
            return
        elapsed = time.monotonic() - self.last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)


class InMemoryDoubanSearchCache:
    def __init__(self) -> None:
        self.results_by_key: dict[str, list[DoubanSearchResult]] = {}

    def get(self, match_input: DoubanMatchInput) -> list[DoubanSearchResult] | None:
        return self.results_by_key.get(search_cache_key(match_input))

    def set(self, match_input: DoubanMatchInput, results: list[DoubanSearchResult]) -> None:
        self.results_by_key[search_cache_key(match_input)] = list(results)


class FileDoubanSearchCache:
    def __init__(self, directory: str | Path = "data/cache/douban-search") -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def get(self, match_input: DoubanMatchInput) -> list[DoubanSearchResult] | None:
        path = self._path_for(match_input)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [_search_result_from_json(item) for item in payload.get("results", [])]

    def set(self, match_input: DoubanMatchInput, results: list[DoubanSearchResult]) -> None:
        payload = {
            "key": search_cache_key(match_input),
            "query": _search_query(match_input),
            "results": [_search_result_to_json(result) for result in results],
        }
        self._path_for(match_input).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _path_for(self, match_input: DoubanMatchInput) -> Path:
        digest = hashlib.sha256(search_cache_key(match_input).encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"


class CachedDoubanSearchAdapter:
    def __init__(self, inner: DoubanSearchAdapter, cache: DoubanSearchCache) -> None:
        self.inner = inner
        self.cache = cache
        self.hit_count = 0
        self.miss_count = 0

    def search(self, match_input: DoubanMatchInput) -> list[DoubanSearchResult]:
        cached = self.cache.get(match_input)
        if cached is not None:
            self.hit_count += 1
            return cached

        self.miss_count += 1
        results = self.inner.search(match_input)
        self.cache.set(match_input, results)
        return results


class InMemoryDoubanMatchRepository:
    def __init__(self) -> None:
        self.candidates_by_raw_id: dict[str, DoubanMatchCandidate] = {}

    def save_all(self, candidates: list[DoubanMatchCandidate]) -> list[DoubanMatchCandidate]:
        for candidate in candidates:
            self.candidates_by_raw_id[candidate.source_raw_id] = candidate
        return candidates

    def all(self) -> list[DoubanMatchCandidate]:
        return list(self.candidates_by_raw_id.values())

    def find_by_status(self, status: DoubanMatchStatus) -> list[DoubanMatchCandidate]:
        return [candidate for candidate in self.all() if candidate.status == status]

    def find_needs_review(self) -> list[DoubanMatchCandidate]:
        return self.find_by_status(DoubanMatchStatus.NEEDS_REVIEW)

    def find_confirmed(self) -> list[DoubanMatchCandidate]:
        return self.find_by_status(DoubanMatchStatus.CONFIRMED)

    def confirm_match(self, source_raw_id: str) -> DoubanMatchCandidate:
        candidate = self._get_existing(source_raw_id)
        if candidate.candidate_subject_id is None:
            raise ValueError("match candidate has no subject id to confirm")

        confirmed = replace(
            candidate,
            status=DoubanMatchStatus.CONFIRMED,
            match_reasons=(*candidate.match_reasons, "human_confirmed"),
        )
        self.candidates_by_raw_id[source_raw_id] = confirmed
        return confirmed

    def set_manual_subject_id(
        self,
        source_raw_id: str,
        subject_id: str,
        title: str | None = None,
        year: int | None = None,
        director: str | None = None,
    ) -> DoubanMatchCandidate:
        candidate = self._get_existing(source_raw_id)
        confirmed = replace(
            candidate,
            status=DoubanMatchStatus.CONFIRMED,
            match_score=1.0,
            match_reasons=("manual_subject_id",),
            candidate_subject_id=subject_id,
            candidate_title=title or candidate.candidate_title or candidate.query_title,
            candidate_year=year if year is not None else candidate.candidate_year,
            candidate_director=director if director is not None else candidate.candidate_director,
        )
        self.candidates_by_raw_id[source_raw_id] = confirmed
        return confirmed

    def _get_existing(self, source_raw_id: str) -> DoubanMatchCandidate:
        candidate = self.candidates_by_raw_id.get(source_raw_id)
        if candidate is None:
            raise KeyError("match candidate not found")
        return candidate


def build_douban_match_inputs(candidates: list[ViewingHistoryCandidate]) -> MatchQueuePreview:
    inputs = [
        DoubanMatchInput(
            source_raw_id=candidate.source_raw_id,
            source_file=candidate.source_file,
            source_row_number=candidate.source_row_number,
            title=candidate.title,
            strategy=SUBJECT_ID_STRATEGY if candidate.douban_subject_id else METADATA_STRATEGY,
            douban_subject_id=candidate.douban_subject_id,
            release_year=candidate.release_year,
            director=candidate.director,
        )
        for candidate in candidates
    ]
    return MatchQueuePreview(inputs=inputs)


def build_confirmed_viewing_history_inputs(
    candidates: list[ViewingHistoryCandidate],
    confirmed_matches: list[DoubanMatchCandidate],
) -> list[ConfirmedViewingHistoryInput]:
    return _build_viewing_history_inputs(candidates, confirmed_matches, {DoubanMatchStatus.CONFIRMED})


def build_auto_matched_viewing_history_inputs(
    candidates: list[ViewingHistoryCandidate],
    match_candidates: list[DoubanMatchCandidate],
) -> list[ConfirmedViewingHistoryInput]:
    return _build_viewing_history_inputs(candidates, match_candidates, {DoubanMatchStatus.AUTO_MATCHED})


def _build_viewing_history_inputs(
    candidates: list[ViewingHistoryCandidate],
    matches: list[DoubanMatchCandidate],
    accepted_statuses: set[DoubanMatchStatus],
) -> list[ConfirmedViewingHistoryInput]:
    candidates_by_raw_id = {candidate.source_raw_id: candidate for candidate in candidates}
    inputs: list[ConfirmedViewingHistoryInput] = []

    for match in matches:
        if match.status not in accepted_statuses or match.candidate_subject_id is None:
            continue

        candidate = candidates_by_raw_id.get(match.source_raw_id)
        if candidate is None:
            continue

        inputs.append(
            ConfirmedViewingHistoryInput(
                source_raw_id=candidate.source_raw_id,
                source_file=candidate.source_file,
                source_row_number=candidate.source_row_number,
                douban_subject_id=match.candidate_subject_id,
                watched_date=candidate.watched_date,
                user_rating=candidate.user_rating,
                source_row_hash=candidate.source_row_hash,
                quality=candidate.quality,
                comment=candidate.comment,
            )
        )

    return inputs


def search_cache_key(match_input: DoubanMatchInput) -> str:
    return _normalize_search_key_part(match_input.title)


def parse_douban_search_results(html: str, limit: int = 5) -> list[DoubanSearchResult]:
    results: list[DoubanSearchResult] = []
    seen_subject_ids: set[str] = set()
    anchor_pattern = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>", re.IGNORECASE | re.DOTALL)

    for match in anchor_pattern.finditer(html):
        subject_id = _extract_subject_id_from_anchor_attrs(match.group("attrs"))
        if subject_id is None:
            continue
        if subject_id in seen_subject_ids:
            continue

        title = _clean_html_text(match.group("body"))
        if not title:
            continue

        context = html[match.end() : match.end() + 900]
        results.append(
            DoubanSearchResult(
                subject_id=subject_id,
                title=title,
                year=_extract_year(context),
                director=_extract_director(context),
                url=f"https://movie.douban.com/subject/{subject_id}/",
            )
        )
        seen_subject_ids.add(subject_id)
        if len(results) >= limit:
            break

    return results


def run_local_match_rules(inputs: list[DoubanMatchInput]) -> MatchRunResult:
    candidates = [_to_match_candidate(item) for item in inputs]
    return MatchRunResult(candidates=candidates)


def run_search_match_job(inputs: list[DoubanMatchInput], adapter: DoubanSearchAdapter) -> MatchRunResult:
    candidates: list[DoubanMatchCandidate] = []
    for match_input in inputs:
        if match_input.douban_subject_id:
            candidates.append(_to_match_candidate(match_input))
            continue

        search_results = adapter.search(match_input)
        if not search_results:
            candidates.append(_to_no_match_candidate(match_input))
            continue

        selected_result = _select_first_year_match(match_input, search_results)
        if selected_result is None:
            candidates.append(_to_review_candidate(match_input, search_results[0], reason="douban_search_no_year_match"))
            continue

        candidates.append(_to_scored_search_candidate(match_input, selected_result))
    return MatchRunResult(candidates=candidates)


def score_search_result(match_input: DoubanMatchInput, search_result: DoubanSearchResult) -> SearchMatchScore:
    title_matches = normalize_title(match_input.title) == normalize_title(search_result.title)
    year_matches = match_input.release_year is None or search_result.year == match_input.release_year
    if title_matches and year_matches:
        return SearchMatchScore(
            status=DoubanMatchStatus.AUTO_MATCHED,
            score=0.95,
            reason="title_year_exact",
        )
    if title_matches:
        return SearchMatchScore(
            status=DoubanMatchStatus.AUTO_MATCHED,
            score=0.9,
            reason="title_exact_year_differs",
        )
    if not year_matches:
        return SearchMatchScore(
            status=DoubanMatchStatus.NEEDS_REVIEW,
            score=0.5,
            reason="year_mismatch",
        )

    return SearchMatchScore(
        status=DoubanMatchStatus.NEEDS_REVIEW,
        score=0.75,
        reason="year_match_title_differs",
    )


def _to_match_candidate(match_input: DoubanMatchInput) -> DoubanMatchCandidate:
    if match_input.douban_subject_id:
        return DoubanMatchCandidate(
            source_raw_id=match_input.source_raw_id,
            source_file=match_input.source_file,
            source_row_number=match_input.source_row_number,
            query_title=match_input.title,
            status=DoubanMatchStatus.AUTO_MATCHED,
            match_score=1.0,
            match_reasons=("excel_douban_subject_id",),
            candidate_subject_id=match_input.douban_subject_id,
            candidate_title=match_input.title,
            candidate_year=match_input.release_year,
            candidate_director=match_input.director,
        )

    return DoubanMatchCandidate(
        source_raw_id=match_input.source_raw_id,
        source_file=match_input.source_file,
        source_row_number=match_input.source_row_number,
        query_title=match_input.title,
        status=DoubanMatchStatus.NEEDS_REVIEW,
        match_score=0.0,
        match_reasons=("metadata_match_required",),
        candidate_title=match_input.title,
        candidate_year=match_input.release_year,
        candidate_director=match_input.director,
    )


def _to_no_match_candidate(match_input: DoubanMatchInput, reason: str = "douban_search_no_results") -> DoubanMatchCandidate:
    return DoubanMatchCandidate(
        source_raw_id=match_input.source_raw_id,
        source_file=match_input.source_file,
        source_row_number=match_input.source_row_number,
        query_title=match_input.title,
        status=DoubanMatchStatus.NO_MATCH,
        match_score=0.0,
        match_reasons=(reason,),
        candidate_title=match_input.title,
        candidate_year=match_input.release_year,
        candidate_director=match_input.director,
    )


def _to_review_candidate(
    match_input: DoubanMatchInput,
    search_result: DoubanSearchResult,
    reason: str = "douban_search_candidate",
) -> DoubanMatchCandidate:
    return DoubanMatchCandidate(
        source_raw_id=match_input.source_raw_id,
        source_file=match_input.source_file,
        source_row_number=match_input.source_row_number,
        query_title=match_input.title,
        status=DoubanMatchStatus.NEEDS_REVIEW,
        match_score=0.0,
        match_reasons=(reason,),
        candidate_subject_id=search_result.subject_id,
        candidate_title=search_result.title,
        candidate_year=search_result.year,
        candidate_director=search_result.director,
    )


def _to_scored_search_candidate(match_input: DoubanMatchInput, search_result: DoubanSearchResult) -> DoubanMatchCandidate:
    score = score_search_result(match_input, search_result)
    return DoubanMatchCandidate(
        source_raw_id=match_input.source_raw_id,
        source_file=match_input.source_file,
        source_row_number=match_input.source_row_number,
        query_title=match_input.title,
        status=score.status,
        match_score=score.score,
        match_reasons=(score.reason,),
        candidate_subject_id=search_result.subject_id,
        candidate_title=search_result.title,
        candidate_year=search_result.year,
        candidate_director=search_result.director,
    )


def _select_first_year_match(
    match_input: DoubanMatchInput,
    search_results: list[DoubanSearchResult],
) -> DoubanSearchResult | None:
    title_matches = [
        result
        for result in search_results
        if normalize_title(result.title) == normalize_title(match_input.title)
    ]
    if title_matches:
        if match_input.release_year is None:
            return title_matches[0]
        return next((result for result in title_matches if result.year == match_input.release_year), title_matches[0])

    if match_input.release_year is None:
        return search_results[0] if search_results else None
    return next((result for result in search_results if result.year == match_input.release_year), None)


def _normalize_search_key_part(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(value.casefold().split())


def normalize_title(value: str) -> str:
    return " ".join(value.casefold().split())


def _extract_subject_id_from_anchor_attrs(attrs: str) -> str | None:
    decoded_attrs = unquote(unescape(attrs))
    url_match = re.search(r"movie\.douban\.com/subject/(\d+)/?", decoded_attrs)
    if url_match is not None:
        return url_match.group(1)

    sid_match = re.search(r"\bsid:\s*(\d+)", decoded_attrs)
    if sid_match is not None:
        return sid_match.group(1)

    return None


def _search_query(match_input: DoubanMatchInput) -> str:
    return match_input.title


def _clean_html_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(without_tags).split())


def _extract_year(context: str) -> int | None:
    text = _clean_html_text(context)
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if match is None:
        return None
    return int(match.group(1))


def _extract_director(context: str) -> str | None:
    text = _clean_html_text(context)
    match = re.search(r"导演[:：]\s*([^/]+)", text)
    if match is None:
        return None
    return match.group(1).strip() or None


def _search_result_to_json(result: DoubanSearchResult) -> dict[str, str | int | None]:
    return {
        "subject_id": result.subject_id,
        "title": result.title,
        "year": result.year,
        "director": result.director,
        "url": result.url,
    }


def _search_result_from_json(payload: dict[str, str | int | None]) -> DoubanSearchResult:
    return DoubanSearchResult(
        subject_id=str(payload["subject_id"]),
        title=str(payload["title"]),
        year=int(payload["year"]) if payload.get("year") is not None else None,
        director=str(payload["director"]) if payload.get("director") is not None else None,
        url=str(payload["url"]) if payload.get("url") is not None else None,
    )
