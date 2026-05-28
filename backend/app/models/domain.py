from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from uuid import uuid4


class FeedbackType(str, Enum):
    WANT_TO_WATCH = "want_to_watch"
    MAYBE_LATER = "maybe_later"
    NOT_INTERESTED = "not_interested"
    OPENED_DOUBAN = "opened_douban"


class SlotType(str, Enum):
    EXPLOIT = "exploit"
    EXPLORE = "explore"


class WishlistStatus(str, Enum):
    ACTIVE = "active"
    WATCHED = "watched"
    REMOVED = "removed"


class DoubanMatchStatus(str, Enum):
    AUTO_MATCHED = "auto_matched"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    NO_MATCH = "no_match"


@dataclass(frozen=True)
class ViewingHistoryRaw:
    source_sheet_name: str
    source_row_number: int
    source_row_checksum: str
    date_raw: str | None
    name_raw: str | None
    director_raw: str | None
    year_raw: str | None
    rating_raw: str | None
    quality_raw: str | None
    comment_raw: str | None
    douban_subject_id_raw: str | None = None
    douban_image_id_raw: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    imported_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ViewingHistoryCandidate:
    source_raw_id: str
    source_sheet_name: str
    source_row_number: int
    title: str
    user_rating: float
    source_row_checksum: str | None = None
    watched_date: date | None = None
    director: str | None = None
    release_year: int | None = None
    quality: str | None = None
    comment: str | None = None
    douban_subject_id: str | None = None
    douban_image_id: str | None = None


@dataclass(frozen=True)
class DoubanMatchInput:
    source_raw_id: str
    source_sheet_name: str
    source_row_number: int
    title: str
    strategy: str
    douban_subject_id: str | None = None
    release_year: int | None = None
    director: str | None = None


@dataclass(frozen=True)
class DoubanMatchCandidate:
    source_raw_id: str
    source_sheet_name: str
    source_row_number: int
    query_title: str
    status: DoubanMatchStatus
    match_score: float
    match_reasons: tuple[str, ...]
    candidate_subject_id: str | None = None
    candidate_title: str | None = None
    candidate_year: int | None = None
    candidate_director: str | None = None


@dataclass(frozen=True)
class DoubanSearchResult:
    subject_id: str
    title: str
    year: int | None = None
    director: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class ConfirmedViewingHistoryInput:
    source_raw_id: str
    source_sheet_name: str
    source_row_number: int
    douban_subject_id: str
    watched_date: date | None
    user_rating: float
    source_row_checksum: str | None = None
    quality: str | None = None
    comment: str | None = None


@dataclass(frozen=True)
class DoubanMovieDetail:
    subject_id: str
    title: str
    display_title: str | None = None
    original_title: str | None = None
    aka_titles: tuple[str, ...] = ()
    year: int | None = None
    directors: tuple[str, ...] = ()
    actors: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    douban_rating: float | None = None
    douban_vote_count: int | None = None
    summary: str | None = None
    poster_url: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Movie:
    id: str
    title: str
    year: int
    directors: tuple[str, ...]
    actors: tuple[str, ...]
    genres: tuple[str, ...]
    countries: tuple[str, ...]
    douban_rating: float
    douban_vote_count: int
    douban_url: str
    awards: tuple[str, ...] = ()

    @property
    def decade(self) -> int:
        return self.year - self.year % 10


@dataclass
class ViewingHistory:
    movie_id: str
    watched_date: date
    user_rating: float | None
    quality: str | None = None
    comment: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RecommendationItem:
    movie: Movie
    rank: int
    slot_type: SlotType
    score: float
    score_components: dict[str, float]
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class RecommendationSession:
    strategy: str
    items: list[RecommendationItem]
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Feedback:
    session_id: str
    item_id: str
    movie_id: str
    feedback_type: FeedbackType
    feedback_value: float
    comment: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class WishlistItem:
    movie: Movie
    source_session_id: str
    status: WishlistStatus = WishlistStatus.ACTIVE
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None
