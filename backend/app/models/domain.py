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
