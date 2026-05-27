from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
import os
import random
from typing import Any, Literal, Protocol

from backend.app.config import load_local_env
from backend.app.models.domain import (
    Feedback,
    FeedbackType,
    Movie,
    RecommendationItem,
    RecommendationSession,
    SlotType,
    ViewingHistory,
    WishlistItem,
    WishlistStatus,
)
from backend.app.recommenders.simple import content_score, diversity_gain, hybrid_score, popularity_score
from backend.app.services.catalog import seed_history, seed_movies

load_local_env()

Strategy = Literal["popularity", "content", "hybrid"]


@dataclass
class FeedbackRequest:
    feedback_type: FeedbackType
    comment: str | None = None


@dataclass
class RecordWatchedRequest:
    watched_date: date
    user_rating: float
    quality: str | None = None
    comment: str | None = None


class MovieRepository(Protocol):
    movies_by_id: dict[str, Movie]
    history: list[ViewingHistory]

    def active_candidates(self) -> list[Movie]: ...

    def save_session(self, session: RecommendationSession) -> RecommendationSession: ...

    def get_session(self, session_id: str) -> RecommendationSession | None: ...

    def add_feedback(self, feedback: Feedback) -> Feedback: ...

    def add_to_wishlist(self, movie: Movie, session_id: str) -> WishlistItem: ...

    def list_active_wishlist(self) -> list[WishlistItem]: ...

    def find_wishlist_item(self, wishlist_id: str) -> WishlistItem | None: ...

    def mark_wishlist_watched(self, wishlist_item: WishlistItem) -> WishlistItem: ...

    def add_viewing_history(self, history: ViewingHistory, wishlist_id: str) -> ViewingHistory: ...


class InMemoryMovieRepository:
    def __init__(self, movies: list[Movie] | None = None, history: list[ViewingHistory] | None = None) -> None:
        self.movies_by_id = {movie.id: movie for movie in movies or seed_movies()}
        self.candidate_pool = set(self.movies_by_id)
        self.history = list(history or seed_history())
        self.sessions: dict[str, RecommendationSession] = {}
        self.feedback: list[Feedback] = []
        self.wishlist: dict[str, WishlistItem] = {}

    def active_candidates(self) -> list[Movie]:
        watched_movie_ids = {item.movie_id for item in self.history}
        active_wishlist_movie_ids = {
            item.movie.id for item in self.wishlist.values() if item.status == WishlistStatus.ACTIVE
        }
        return [
            movie
            for movie_id, movie in self.movies_by_id.items()
            if movie_id in self.candidate_pool
            and movie_id not in watched_movie_ids
            and movie_id not in active_wishlist_movie_ids
            and not self._has_hard_negative(movie_id)
        ]

    def save_session(self, session: RecommendationSession) -> RecommendationSession:
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> RecommendationSession | None:
        return self.sessions.get(session_id)

    def add_feedback(self, feedback: Feedback) -> Feedback:
        self.feedback.append(feedback)
        return feedback

    def add_to_wishlist(self, movie: Movie, session_id: str) -> WishlistItem:
        existing = self.find_active_wishlist_by_movie(movie.id)
        if existing:
            return existing
        item = WishlistItem(movie=movie, source_session_id=session_id)
        self.wishlist[item.id] = item
        return item

    def find_active_wishlist_by_movie(self, movie_id: str) -> WishlistItem | None:
        return next(
            (item for item in self.wishlist.values() if item.movie.id == movie_id and item.status == WishlistStatus.ACTIVE),
            None,
        )

    def list_active_wishlist(self) -> list[WishlistItem]:
        return [item for item in self.wishlist.values() if item.status == WishlistStatus.ACTIVE]

    def find_wishlist_item(self, wishlist_id: str) -> WishlistItem | None:
        return self.wishlist.get(wishlist_id)

    def mark_wishlist_watched(self, wishlist_item: WishlistItem) -> WishlistItem:
        wishlist_item.status = WishlistStatus.WATCHED
        wishlist_item.closed_at = datetime.now(timezone.utc)
        return wishlist_item

    def add_viewing_history(self, history: ViewingHistory, wishlist_id: str) -> ViewingHistory:
        self.history.append(history)
        return history

    def _has_hard_negative(self, movie_id: str) -> bool:
        return any(item.movie_id == movie_id and item.feedback_type == FeedbackType.NOT_INTERESTED for item in self.feedback)


class PostgresRecommendationRepository:
    def __init__(self, dsn: str) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self.connection = psycopg.connect(dsn, row_factory=dict_row)
        self.connection.autocommit = True
        self.movies_by_id: dict[str, Movie] = {}
        self.history: list[ViewingHistory] = []
        self.sessions: dict[str, RecommendationSession] = {}
        self.feedback: list[Feedback] = []
        self.wishlist: dict[str, WishlistItem] = {}
        self._active_candidate_movie_ids: set[str] = set()
        self._initialize_interaction_schema()

    def close(self) -> None:
        self.connection.close()

    def active_candidates(self) -> list[Movie]:
        self.refresh()
        active_wishlist_movie_ids = {
            item.movie.id for item in self.wishlist.values() if item.status == WishlistStatus.ACTIVE
        }
        return [
            movie
            for movie_id, movie in self.movies_by_id.items()
            if movie_id in self._active_candidate_movie_ids
            and movie_id not in active_wishlist_movie_ids
            and not self._has_hard_negative(movie_id)
        ]

    def refresh(self) -> None:
        movie_rows = self.connection.execute(
            """
            SELECT
                id,
                douban_subject_id,
                douban_url,
                title,
                year,
                directors,
                actors,
                genres,
                countries,
                douban_rating,
                douban_vote_count
            FROM movies
            """
        ).fetchall()
        self.movies_by_id = {str(row["id"]): self._movie_from_row(row) for row in movie_rows}

        history_rows = self.connection.execute(
            """
            SELECT movie_id, watched_date, user_rating, quality, comment, id, created_at
            FROM viewing_history
            ORDER BY watched_date, created_at
            """
        ).fetchall()
        self.history = [
            ViewingHistory(
                movie_id=str(row["movie_id"]),
                watched_date=row["watched_date"],
                user_rating=float(row["user_rating"]) if row["user_rating"] is not None else None,
                quality=row["quality"],
                comment=row["comment"],
                id=str(row["id"]),
                created_at=row["created_at"],
            )
            for row in history_rows
        ]

        candidate_rows = self.connection.execute(
            """
            SELECT DISTINCT cp.movie_id
            FROM candidate_pool cp
            WHERE cp.active = TRUE
              AND NOT EXISTS (
                  SELECT 1
                  FROM viewing_history vh
                  WHERE vh.movie_id = cp.movie_id
              )
            """
        ).fetchall()
        self._active_candidate_movie_ids = {str(row["movie_id"]) for row in candidate_rows}
        self._load_interaction_state()

    def save_session(self, session: RecommendationSession) -> RecommendationSession:
        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO recommendation_sessions (id, strategy, context_snapshot, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    strategy = excluded.strategy,
                    context_snapshot = excluded.context_snapshot
                """,
                (session.id, session.strategy, self._jsonb({}), session.created_at),
            )
            for item in session.items:
                self.connection.execute(
                    """
                    INSERT INTO recommendation_items (
                        id, session_id, movie_id, rank, slot_type, score, score_components, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(id) DO UPDATE SET
                        rank = excluded.rank,
                        slot_type = excluded.slot_type,
                        score = excluded.score,
                        score_components = excluded.score_components
                    """,
                    (
                        item.id,
                        session.id,
                        item.movie.id,
                        item.rank,
                        item.slot_type.value,
                        item.score,
                        self._jsonb(item.score_components),
                        session.created_at,
                    ),
                )
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> RecommendationSession | None:
        cached = self.sessions.get(session_id)
        if cached is not None:
            return cached

        row = self.connection.execute(
            """
            SELECT id, strategy, created_at
            FROM recommendation_sessions
            WHERE id = %s
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None

        self.refresh()
        item_rows = self.connection.execute(
            """
            SELECT id, movie_id, rank, slot_type, score, score_components
            FROM recommendation_items
            WHERE session_id = %s
            ORDER BY rank
            """,
            (session_id,),
        ).fetchall()
        items: list[RecommendationItem] = []
        for item_row in item_rows:
            movie = self.movies_by_id.get(str(item_row["movie_id"]))
            if movie is None:
                continue
            items.append(
                RecommendationItem(
                    movie=movie,
                    rank=int(item_row["rank"]),
                    slot_type=SlotType(str(item_row["slot_type"])),
                    score=float(item_row["score"]),
                    score_components=self._dict_from_json_value(item_row["score_components"]),
                    id=str(item_row["id"]),
                )
            )
        session = RecommendationSession(
            strategy=str(row["strategy"]),
            items=items,
            id=str(row["id"]),
            created_at=row["created_at"],
        )
        self.sessions[session.id] = session
        return session

    def add_feedback(self, feedback: Feedback) -> Feedback:
        self.connection.execute(
            """
            INSERT INTO feedback (
                id, session_id, item_id, movie_id, feedback_type, feedback_value, comment, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                feedback.id,
                feedback.session_id,
                feedback.item_id,
                feedback.movie_id,
                feedback.feedback_type.value,
                feedback.feedback_value,
                feedback.comment,
                feedback.created_at,
            ),
        )
        self.feedback.append(feedback)
        return feedback

    def add_to_wishlist(self, movie: Movie, session_id: str) -> WishlistItem:
        existing = self.find_active_wishlist_by_movie(movie.id)
        if existing:
            return existing
        item = WishlistItem(movie=movie, source_session_id=session_id)
        self.connection.execute(
            """
            INSERT INTO wishlist (id, movie_id, source_session_id, status, created_at, closed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO NOTHING
            """,
            (item.id, movie.id, session_id, item.status.value, item.created_at, item.closed_at),
        )
        self.wishlist[item.id] = item
        return item

    def find_active_wishlist_by_movie(self, movie_id: str) -> WishlistItem | None:
        self._load_wishlist()
        return next(
            (item for item in self.wishlist.values() if item.movie.id == movie_id and item.status == WishlistStatus.ACTIVE),
            None,
        )

    def list_active_wishlist(self) -> list[WishlistItem]:
        self.refresh()
        return [item for item in self.wishlist.values() if item.status == WishlistStatus.ACTIVE]

    def find_wishlist_item(self, wishlist_id: str) -> WishlistItem | None:
        self.refresh()
        return self.wishlist.get(wishlist_id)

    def mark_wishlist_watched(self, wishlist_item: WishlistItem) -> WishlistItem:
        wishlist_item.status = WishlistStatus.WATCHED
        wishlist_item.closed_at = datetime.now(timezone.utc)
        self.connection.execute(
            """
            UPDATE wishlist
            SET status = %s, closed_at = %s
            WHERE id = %s
            """,
            (wishlist_item.status.value, wishlist_item.closed_at, wishlist_item.id),
        )
        self.wishlist[wishlist_item.id] = wishlist_item
        return wishlist_item

    def add_viewing_history(self, history: ViewingHistory, wishlist_id: str) -> ViewingHistory:
        now = history.created_at
        self.connection.execute(
            """
            INSERT INTO viewing_history (
                id, movie_id, watched_date, user_rating, quality, comment,
                source_row_hash, source_file, source_row_number, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'wishlist', 0, %s, %s)
            ON CONFLICT(source_row_hash) DO UPDATE SET
                movie_id = excluded.movie_id,
                watched_date = excluded.watched_date,
                user_rating = excluded.user_rating,
                quality = excluded.quality,
                comment = excluded.comment,
                updated_at = excluded.updated_at
            """,
            (
                history.id,
                history.movie_id,
                history.watched_date,
                history.user_rating,
                history.quality,
                history.comment,
                f"wishlist:{wishlist_id}",
                now,
                now,
            ),
        )
        self.history.append(history)
        return history

    def _has_hard_negative(self, movie_id: str) -> bool:
        return any(item.movie_id == movie_id and item.feedback_type == FeedbackType.NOT_INTERESTED for item in self.feedback)

    def _movie_from_row(self, row: dict[str, Any]) -> Movie:
        subject_id = row["douban_subject_id"]
        douban_url = row["douban_url"] or (
            f"https://movie.douban.com/subject/{subject_id}/" if subject_id else ""
        )
        return Movie(
            id=str(row["id"]),
            title=row["title"],
            year=int(row["year"] or 0),
            directors=self._tuple_from_json_value(row["directors"]),
            actors=self._tuple_from_json_value(row["actors"]),
            genres=self._tuple_from_json_value(row["genres"]),
            countries=self._tuple_from_json_value(row["countries"]),
            douban_rating=float(row["douban_rating"] or 0),
            douban_vote_count=int(row["douban_vote_count"] or 0),
            douban_url=douban_url,
        )

    def _tuple_from_json_value(self, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = json.loads(value)
        return tuple(str(item) for item in value)

    def _dict_from_json_value(self, value: Any) -> dict[str, float]:
        if value is None:
            return {}
        if isinstance(value, str):
            value = json.loads(value)
        return {str(key): float(score) for key, score in dict(value).items()}

    def _load_interaction_state(self) -> None:
        self._load_feedback()
        self._load_wishlist()

    def _load_feedback(self) -> None:
        rows = self.connection.execute(
            """
            SELECT id, session_id, item_id, movie_id, feedback_type, feedback_value, comment, created_at
            FROM feedback
            ORDER BY created_at
            """
        ).fetchall()
        self.feedback = [
            Feedback(
                session_id=str(row["session_id"]),
                item_id=str(row["item_id"]),
                movie_id=str(row["movie_id"]),
                feedback_type=FeedbackType(str(row["feedback_type"])),
                feedback_value=float(row["feedback_value"]),
                comment=row["comment"],
                id=str(row["id"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _load_wishlist(self) -> None:
        rows = self.connection.execute(
            """
            SELECT id, movie_id, source_session_id, status, created_at, closed_at
            FROM wishlist
            ORDER BY created_at
            """
        ).fetchall()
        if not self.movies_by_id:
            movie_rows = self.connection.execute(
                """
                SELECT
                    id, douban_subject_id, douban_url, title, year, directors, actors,
                    genres, countries, douban_rating, douban_vote_count
                FROM movies
                """
            ).fetchall()
            self.movies_by_id = {str(row["id"]): self._movie_from_row(row) for row in movie_rows}
        self.wishlist = {}
        for row in rows:
            movie = self.movies_by_id.get(str(row["movie_id"]))
            if movie is None:
                continue
            item = WishlistItem(
                movie=movie,
                source_session_id=str(row["source_session_id"]),
                status=WishlistStatus(str(row["status"])),
                id=str(row["id"]),
                created_at=row["created_at"],
                closed_at=row["closed_at"],
            )
            self.wishlist[item.id] = item

    def _initialize_interaction_schema(self) -> None:
        with self.connection.transaction():
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_sessions (
                    id UUID PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    context_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_items (
                    id UUID PRIMARY KEY,
                    session_id UUID NOT NULL REFERENCES recommendation_sessions(id),
                    movie_id UUID NOT NULL REFERENCES movies(id),
                    rank INTEGER NOT NULL,
                    slot_type TEXT NOT NULL,
                    score NUMERIC NOT NULL,
                    score_components JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL,
                    UNIQUE(session_id, rank)
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    id UUID PRIMARY KEY,
                    session_id UUID NOT NULL REFERENCES recommendation_sessions(id),
                    item_id UUID NOT NULL REFERENCES recommendation_items(id),
                    movie_id UUID NOT NULL REFERENCES movies(id),
                    feedback_type TEXT NOT NULL,
                    feedback_value NUMERIC NOT NULL,
                    comment TEXT,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS wishlist (
                    id UUID PRIMARY KEY,
                    movie_id UUID NOT NULL REFERENCES movies(id),
                    source_session_id UUID NOT NULL REFERENCES recommendation_sessions(id),
                    status TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    closed_at TIMESTAMPTZ
                )
                """
            )

    def _jsonb(self, value: Any):
        from psycopg.types.json import Jsonb

        return Jsonb(value)


class RecommendationService:
    def __init__(self, repository: MovieRepository, explore_pool_size: int = 10) -> None:
        self.repository = repository
        self.explore_pool_size = explore_pool_size

    def recommend(self, strategy: Strategy = "hybrid", explore_seed: int | None = None) -> RecommendationSession:
        candidates = self.repository.active_candidates()
        if len(candidates) < 5:
            raise ValueError("At least five eligible candidates are required")

        scored = [(movie, self._score(movie, strategy)) for movie in candidates]
        scored.sort(key=lambda item: item[1]["total"], reverse=True)

        exploit_movies = [movie for movie, _ in scored[:3]]
        selected = list(exploit_movies)
        remaining = [(movie, scores) for movie, scores in scored if movie not in selected]
        explore_movies = self._select_explore(remaining, selected, limit=2, explore_seed=explore_seed)
        selected.extend(explore_movies)

        items = [
            RecommendationItem(
                movie=movie,
                rank=index + 1,
                slot_type=SlotType.EXPLOIT if index < 3 else SlotType.EXPLORE,
                score=self._score(movie, strategy)["total"],
                score_components=self._score(movie, strategy),
            )
            for index, movie in enumerate(selected)
        ]
        return self.repository.save_session(RecommendationSession(strategy=strategy, items=items))

    def submit_feedback(self, session_id: str, item_id: str, request: FeedbackRequest) -> Feedback:
        session = self.repository.get_session(session_id)
        if session is None:
            raise KeyError("recommendation session not found")
        item = next((candidate for candidate in session.items if candidate.id == item_id), None)
        if item is None:
            raise KeyError("recommendation item not found")

        feedback = self.repository.add_feedback(
            Feedback(
                session_id=session_id,
                item_id=item_id,
                movie_id=item.movie.id,
                feedback_type=request.feedback_type,
                feedback_value=self._feedback_value(request.feedback_type),
                comment=request.comment,
            )
        )
        if request.feedback_type == FeedbackType.WANT_TO_WATCH:
            self.repository.add_to_wishlist(item.movie, session_id)
        return feedback

    def record_watched(self, wishlist_id: str, request: RecordWatchedRequest) -> ViewingHistory:
        wishlist_item = self.repository.find_wishlist_item(wishlist_id)
        if wishlist_item is None:
            raise KeyError("wishlist item not found")
        if wishlist_item.status != WishlistStatus.ACTIVE:
            raise ValueError("wishlist item is not active")

        wishlist_item = self.repository.mark_wishlist_watched(wishlist_item)
        history = ViewingHistory(
            movie_id=wishlist_item.movie.id,
            watched_date=request.watched_date,
            user_rating=request.user_rating,
            quality=request.quality,
            comment=request.comment,
        )
        return self.repository.add_viewing_history(history, wishlist_id)

    def _score(self, movie: Movie, strategy: Strategy) -> dict[str, float]:
        if strategy == "popularity":
            total = popularity_score(movie)
            return {"public_quality": total, "total": total}
        if strategy == "content":
            total = content_score(movie, self.repository.history, self.repository.movies_by_id)
            return {"personal_preference": total, "total": total}
        if strategy == "hybrid":
            return hybrid_score(movie, self.repository.history, self.repository.movies_by_id)
        raise ValueError(f"Unknown recommendation strategy: {strategy}")

    def _select_explore(
        self,
        scored: list[tuple[Movie, dict[str, float]]],
        selected: list[Movie],
        limit: int,
        explore_seed: int | None = None,
    ) -> list[Movie]:
        rng = random.Random(explore_seed) if explore_seed is not None else random.Random()
        explore: list[Movie] = []
        for _ in range(limit):
            if not scored:
                break
            ranked = sorted(
                (
                    (movie, scores, diversity_gain(movie, selected + explore) * 0.65 + scores["total"] * 0.35)
                    for movie, scores in scored
                ),
                key=lambda item: item[2],
                reverse=True,
            )
            sample_pool = ranked[: max(limit, self.explore_pool_size)]
            weights = _positive_weights([item[2] for item in sample_pool])
            movie = rng.choices([item[0] for item in sample_pool], weights=weights, k=1)[0]
            scored = [(candidate, scores) for candidate, scores in scored if candidate.id != movie.id]
            explore.append(movie)
        return explore

    def _feedback_value(self, feedback_type: FeedbackType) -> float:
        return {
            FeedbackType.WANT_TO_WATCH: 0.7,
            FeedbackType.MAYBE_LATER: 0.2,
            FeedbackType.NOT_INTERESTED: -0.8,
            FeedbackType.OPENED_DOUBAN: 0.1,
        }[feedback_type]

    def to_session_response(self, session: RecommendationSession) -> dict:
        return {
            "id": session.id,
            "strategy": session.strategy,
            "created_at": session.created_at.isoformat(),
            "items": [
                {
                    "id": item.id,
                    "rank": item.rank,
                    "slot_type": item.slot_type.value,
                    "score": item.score,
                    "score_components": item.score_components,
                    "movie": self._movie_response(item.movie),
                }
                for item in session.items
            ],
        }

    def to_feedback_response(self, feedback: Feedback) -> dict:
        return {
            "id": feedback.id,
            "session_id": feedback.session_id,
            "item_id": feedback.item_id,
            "movie_id": feedback.movie_id,
            "feedback_type": feedback.feedback_type.value,
            "feedback_value": feedback.feedback_value,
            "comment": feedback.comment,
            "created_at": feedback.created_at.isoformat(),
        }

    def to_wishlist_response(self) -> dict:
        return {
            "items": [
                {
                    "id": item.id,
                    "status": item.status.value,
                    "source_session_id": item.source_session_id,
                    "created_at": item.created_at.isoformat(),
                    "closed_at": item.closed_at.isoformat() if item.closed_at else None,
                    "movie": self._movie_response(item.movie),
                }
                for item in self.repository.list_active_wishlist()
            ]
        }

    def to_viewing_history_response(self, history: ViewingHistory) -> dict:
        response = asdict(history)
        response["watched_date"] = history.watched_date.isoformat()
        response["created_at"] = history.created_at.isoformat()
        return response

    def _movie_response(self, movie: Movie) -> dict:
        return {
            "id": movie.id,
            "title": movie.title,
            "year": movie.year,
            "director": ", ".join(movie.directors),
            "main_cast": list(movie.actors[:3]),
            "douban_rating": movie.douban_rating,
            "awards": list(movie.awards),
            "douban_url": movie.douban_url,
        }


def create_recommendation_service() -> RecommendationService:
    backend = os.getenv("MOVIES_RECOMMENDATION_BACKEND", "memory").lower()
    if backend == "postgres":
        dsn = os.getenv("MOVIES_POSTGRES_DSN")
        if not dsn:
            raise RuntimeError("MOVIES_POSTGRES_DSN is required when MOVIES_RECOMMENDATION_BACKEND=postgres")
        return RecommendationService(PostgresRecommendationRepository(dsn))
    if backend == "memory":
        return RecommendationService(InMemoryMovieRepository())
    raise RuntimeError(f"Unknown MOVIES_RECOMMENDATION_BACKEND: {backend}")


service = create_recommendation_service()


def _positive_weights(values: list[float]) -> list[float]:
    if not values:
        return []
    minimum = min(values)
    shifted = [value - minimum + 0.001 for value in values]
    if any(value > 0 for value in shifted):
        return shifted
    return [1.0 for _ in values]
