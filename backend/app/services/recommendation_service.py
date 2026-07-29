from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
import random
from threading import RLock
from typing import Any, Literal, Protocol

from backend.app.config import load_local_env
from backend.app.db.postgres_repository import initialize_interaction_schema
from backend.app.models.domain import (
    Feedback,
    FeedbackType,
    Movie,
    RecommendationItem,
    RecommendationProcessingStatus,
    RecommendationSession,
    SlotType,
    ViewingHistory,
    WishlistItem,
    WishlistStatus,
)
from backend.app.recommenders.simple import (
    ContentProfile,
    build_content_profile,
    content_score,
    content_score_from_profile,
    diversity_gain,
    hybrid_score,
    popularity_score,
)
from backend.app.recommenders.bandit import (
    BANDIT_MIN_EXAMPLES,
    FEATURE_VERSION,
    REWARD_VERSION,
    build_bandit_feature_context,
    build_bandit_feature_vector,
    build_bandit_training_examples,
    fit_diagonal_linear_thompson_model,
    should_use_bandit_explore,
    write_latest_model_cache,
)
from backend.app.services.catalog import seed_history, seed_movies
from backend.app.services.display_text import display_person_names

load_local_env()

Strategy = Literal["popularity", "content", "hybrid", "bandit_hybrid"]
EXPLOIT_SLOT_COUNT = 4
EXPLORE_SLOT_COUNT = 4
RECOMMENDATION_SESSION_SIZE = EXPLOIT_SLOT_COUNT + EXPLORE_SLOT_COUNT
MAYBE_LATER_DOWNRANKING_WINDOW = timedelta(days=30)


def _current_release_year_limit() -> int:
    return date.today().year


def _is_future_release_year(year: int | None) -> bool:
    return bool(year) and year > _current_release_year_limit()


@dataclass
class RecommendationCandidate:
    movie: Movie
    source_ref: str | None = None
    source_label: str | None = None


@dataclass
class NotInterestedItem:
    movie: Movie
    state_event_id: str
    state_changed_at: datetime
    session_id: str
    item_id: str


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
    feedback: list[Feedback]
    wishlist: dict[str, WishlistItem]

    def active_candidates(self) -> list[RecommendationCandidate]: ...

    def save_session(self, session: RecommendationSession) -> RecommendationSession: ...

    def get_session(self, session_id: str) -> RecommendationSession | None: ...

    def add_feedback(self, feedback: Feedback) -> Feedback: ...

    def add_to_wishlist(self, movie: Movie, session_id: str) -> WishlistItem: ...

    def mark_recommendation_item_processed(
        self,
        session_id: str,
        item_id: str,
        status: RecommendationProcessingStatus,
    ) -> RecommendationItem: ...

    def clear_recommendation_item_processed(self, session_id: str, item_id: str) -> RecommendationItem: ...

    def delete_latest_feedback(self, session_id: str, item_id: str, feedback_type: FeedbackType) -> Feedback | None: ...

    def deactivate_candidate_pool_movie(self, movie_id: str) -> None: ...

    def restore_candidate_pool_movie_if_eligible(self, movie_id: str) -> None: ...

    def list_active_wishlist(self) -> list[WishlistItem]: ...

    def find_active_wishlist_by_movie(self, movie_id: str) -> WishlistItem | None: ...

    def find_wishlist_item(self, wishlist_id: str) -> WishlistItem | None: ...

    def mark_wishlist_watched(self, wishlist_item: WishlistItem) -> WishlistItem: ...

    def remove_wishlist_item(self, wishlist_item: WishlistItem) -> WishlistItem: ...

    def add_viewing_history(self, history: ViewingHistory, wishlist_id: str) -> ViewingHistory: ...

    def list_current_not_interested(self) -> list[NotInterestedItem]: ...

    def find_recommendation_item_by_session_and_movie(
        self,
        session_id: str,
        movie_id: str,
    ) -> RecommendationItem | None: ...

    def recent_recommendation_movie_ids(self, session_count: int) -> set[str]: ...

    def recommendation_training_sessions(self) -> list[RecommendationSession]: ...


class InMemoryMovieRepository:
    def __init__(self, movies: list[Movie] | None = None, history: list[ViewingHistory] | None = None) -> None:
        self.movies_by_id = {movie.id: movie for movie in movies or seed_movies()}
        self.candidate_pool = {
            movie.id: RecommendationCandidate(movie=movie, source_ref=f"top{index}")
            for index, movie in enumerate(self.movies_by_id.values(), start=1)
        }
        self.history = list(history or seed_history())
        self.sessions: dict[str, RecommendationSession] = {}
        self.feedback: list[Feedback] = []
        self.wishlist: dict[str, WishlistItem] = {}

    def active_candidates(self) -> list[RecommendationCandidate]:
        watched_movie_ids = {item.movie_id for item in self.history}
        active_wishlist_movie_ids = {
            item.movie.id for item in self.wishlist.values() if item.status == WishlistStatus.ACTIVE
        }
        return [
            self.candidate_pool[movie_id]
            for movie_id in self.movies_by_id
            if movie_id in self.candidate_pool
            and not _is_future_release_year(self.candidate_pool[movie_id].movie.year)
            and movie_id not in watched_movie_ids
            and movie_id not in active_wishlist_movie_ids
            and self._current_feedback_state(movie_id) != FeedbackType.NOT_INTERESTED
        ]

    def save_session(self, session: RecommendationSession) -> RecommendationSession:
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> RecommendationSession | None:
        return self.sessions.get(session_id)

    def recent_recommendation_movie_ids(self, session_count: int) -> set[str]:
        if session_count <= 0:
            return set()
        recent_sessions = sorted(
            self.sessions.values(),
            key=lambda session: session.created_at,
            reverse=True,
        )[:session_count]
        return {item.movie.id for session in recent_sessions for item in session.items}

    def recommendation_training_sessions(self) -> list[RecommendationSession]:
        return sorted(self.sessions.values(), key=lambda session: session.created_at)

    def add_feedback(self, feedback: Feedback) -> Feedback:
        self.feedback.append(feedback)
        return feedback

    def mark_recommendation_item_processed(
        self,
        session_id: str,
        item_id: str,
        status: RecommendationProcessingStatus,
    ) -> RecommendationItem:
        session = self.sessions[session_id]
        item = next(candidate for candidate in session.items if candidate.id == item_id)
        item.processing_status = status
        item.processed_at = datetime.now(timezone.utc)
        return item

    def clear_recommendation_item_processed(self, session_id: str, item_id: str) -> RecommendationItem:
        session = self.sessions[session_id]
        item = next(candidate for candidate in session.items if candidate.id == item_id)
        item.processing_status = None
        item.processed_at = None
        return item

    def delete_latest_feedback(self, session_id: str, item_id: str, feedback_type: FeedbackType) -> Feedback | None:
        for index in range(len(self.feedback) - 1, -1, -1):
            feedback = self.feedback[index]
            if feedback.session_id == session_id and feedback.item_id == item_id and feedback.feedback_type == feedback_type:
                return self.feedback.pop(index)
        return None

    def deactivate_candidate_pool_movie(self, movie_id: str) -> None:
        self.candidate_pool.pop(movie_id, None)

    def restore_candidate_pool_movie_if_eligible(self, movie_id: str) -> None:
        if movie_id in self.candidate_pool:
            return
        if any(item.movie_id == movie_id for item in self.history):
            return
        if self.find_active_wishlist_by_movie(movie_id) is not None:
            return
        movie = self.movies_by_id.get(movie_id)
        if movie is not None:
            self.candidate_pool[movie_id] = RecommendationCandidate(movie=movie)

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
        return sorted(
            [item for item in self.wishlist.values() if item.status == WishlistStatus.ACTIVE],
            key=lambda item: item.created_at,
            reverse=True,
        )

    def find_wishlist_item(self, wishlist_id: str) -> WishlistItem | None:
        return self.wishlist.get(wishlist_id)

    def mark_wishlist_watched(self, wishlist_item: WishlistItem) -> WishlistItem:
        wishlist_item.status = WishlistStatus.WATCHED
        wishlist_item.closed_at = datetime.now(timezone.utc)
        return wishlist_item

    def remove_wishlist_item(self, wishlist_item: WishlistItem) -> WishlistItem:
        wishlist_item.status = WishlistStatus.REMOVED
        wishlist_item.closed_at = datetime.now(timezone.utc)
        return wishlist_item

    def add_viewing_history(self, history: ViewingHistory, wishlist_id: str) -> ViewingHistory:
        self.history.append(history)
        return history

    def list_current_not_interested(self) -> list[NotInterestedItem]:
        latest_by_movie = self._latest_state_feedback_by_movie()
        items = [
            NotInterestedItem(
                movie=self.movies_by_id[feedback.movie_id],
                state_event_id=feedback.id,
                state_changed_at=feedback.created_at,
                session_id=feedback.session_id,
                item_id=feedback.item_id,
            )
            for feedback in latest_by_movie.values()
            if feedback.feedback_type == FeedbackType.NOT_INTERESTED and feedback.movie_id in self.movies_by_id
        ]
        return sorted(items, key=lambda item: item.state_changed_at, reverse=True)

    def find_recommendation_item_by_session_and_movie(
        self,
        session_id: str,
        movie_id: str,
    ) -> RecommendationItem | None:
        session = self.sessions.get(session_id)
        if session is None:
            return None
        return next((item for item in session.items if item.movie.id == movie_id), None)

    def _current_feedback_state(self, movie_id: str) -> FeedbackType | None:
        latest = self._latest_state_feedback_by_movie().get(movie_id)
        return latest.feedback_type if latest else None

    def _latest_state_feedback_by_movie(self) -> dict[str, Feedback]:
        state_events = {
            FeedbackType.WANT_TO_WATCH,
            FeedbackType.MAYBE_LATER,
            FeedbackType.NOT_INTERESTED,
            FeedbackType.REMOVED_FROM_WISHLIST,
            FeedbackType.CLEAR_NOT_INTERESTED,
        }
        latest: dict[str, Feedback] = {}
        for item in self.feedback:
            if item.feedback_type in state_events:
                latest[item.movie_id] = item
        return latest


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
        self.lock = RLock()
        self._active_candidate_movie_ids: set[str] = set()
        self._active_candidate_sources: dict[str, tuple[str | None, str | None]] = {}
        with self.connection.transaction():
            initialize_interaction_schema(self.connection)

    def close(self) -> None:
        self.connection.close()

    def active_candidates(self) -> list[RecommendationCandidate]:
        self.refresh()
        active_wishlist_movie_ids = {
            item.movie.id for item in self.wishlist.values() if item.status == WishlistStatus.ACTIVE
        }
        return [
            self._candidate_from_movie(movie)
            for movie_id, movie in self.movies_by_id.items()
            if movie_id in self._active_candidate_movie_ids
            and movie_id not in active_wishlist_movie_ids
            and self._current_feedback_state(movie_id) != FeedbackType.NOT_INTERESTED
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
                douban_vote_count,
                poster_url
            FROM movies
            """
        ).fetchall()
        self.movies_by_id = {str(row["id"]): self._movie_from_row(row) for row in movie_rows}

        history_rows = self.connection.execute(
            """
            SELECT movie_id, watched_date, user_rating, quality, comment, id, created_at
            FROM viewing_history
            WHERE deleted_at IS NULL
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
            SELECT DISTINCT ON (cp.movie_id)
                cp.movie_id,
                cp.source_ref,
                COALESCE(
                    cp.source_label,
                    CASE
                        WHEN cp.source_ref LIKE %s AND source_movie.title IS NOT NULL
                        THEN 'recommended from ' || source_movie.title
                        ELSE NULL
                    END
                ) AS source_label
            FROM candidate_pool cp
            JOIN movies candidate_movie
                ON candidate_movie.id = cp.movie_id
            LEFT JOIN movies source_movie
                ON source_movie.douban_subject_id = split_part(cp.source_ref, ':', 2)
            WHERE cp.active = TRUE
              AND (candidate_movie.year IS NULL OR candidate_movie.year <= %s)
              AND NOT EXISTS (
                  SELECT 1
                  FROM viewing_history vh
                  WHERE vh.movie_id = cp.movie_id AND vh.deleted_at IS NULL
              )
            ORDER BY cp.movie_id, cp.updated_at DESC, cp.created_at DESC
            """,
            ("recommended_from:%", _current_release_year_limit()),
        ).fetchall()
        self._active_candidate_movie_ids = {str(row["movie_id"]) for row in candidate_rows}
        self._active_candidate_sources = {
            str(row["movie_id"]): (
                str(row["source_ref"]) if row["source_ref"] is not None else None,
                str(row["source_label"]) if row["source_label"] is not None else None,
            )
            for row in candidate_rows
        }
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
                (session.id, session.strategy, self._jsonb(session.debug_metadata), session.created_at),
            )
            for item in session.items:
                self.connection.execute(
                    """
                    INSERT INTO recommendation_items (
                        id, session_id, movie_id, rank, slot_type, score, score_components,
                        source_ref, source_label, processing_status, processed_at, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(id) DO UPDATE SET
                        rank = excluded.rank,
                        slot_type = excluded.slot_type,
                        score = excluded.score,
                        score_components = excluded.score_components,
                        source_ref = excluded.source_ref,
                        source_label = excluded.source_label,
                        processing_status = excluded.processing_status,
                        processed_at = excluded.processed_at
                    """,
                    (
                        item.id,
                        session.id,
                        item.movie.id,
                        item.rank,
                        item.slot_type.value,
                        item.score,
                        self._jsonb(item.score_components),
                        item.source_ref,
                        item.source_label,
                        item.processing_status.value if item.processing_status else None,
                        item.processed_at,
                        session.created_at,
                    ),
                )
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> RecommendationSession | None:
        row = self.connection.execute(
            """
            SELECT id, strategy, context_snapshot, created_at
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
            SELECT
                id, movie_id, rank, slot_type, score, score_components,
                source_ref, source_label, processing_status, processed_at
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
                    source_ref=str(item_row["source_ref"]) if item_row["source_ref"] is not None else None,
                    source_label=str(item_row["source_label"]) if item_row["source_label"] is not None else None,
                    processing_status=RecommendationProcessingStatus(str(item_row["processing_status"]))
                    if item_row["processing_status"] is not None
                    else None,
                    processed_at=item_row["processed_at"],
                    id=str(item_row["id"]),
                )
            )
        session = RecommendationSession(
            strategy=str(row["strategy"]),
            items=items,
            id=str(row["id"]),
            created_at=row["created_at"],
            debug_metadata=self._dict_from_json_value(row["context_snapshot"]),
        )
        self.sessions[session.id] = session
        return session

    def recommendation_training_sessions(self) -> list[RecommendationSession]:
        self.refresh()
        session_rows = self.connection.execute(
            """
            SELECT id, strategy, context_snapshot, created_at
            FROM recommendation_sessions
            ORDER BY created_at
            """
        ).fetchall()
        if not session_rows:
            return []
        item_rows = self.connection.execute(
            """
            SELECT
                id, session_id, movie_id, rank, slot_type, score, score_components,
                source_ref, source_label, processing_status, processed_at
            FROM recommendation_items
            ORDER BY session_id, rank
            """
        ).fetchall()
        items_by_session: dict[str, list[RecommendationItem]] = {}
        for item_row in item_rows:
            movie = self.movies_by_id.get(str(item_row["movie_id"]))
            if movie is None:
                continue
            session_id = str(item_row["session_id"])
            items_by_session.setdefault(session_id, []).append(
                RecommendationItem(
                    movie=movie,
                    rank=int(item_row["rank"]),
                    slot_type=SlotType(str(item_row["slot_type"])),
                    score=float(item_row["score"]),
                    score_components=self._dict_from_json_value(item_row["score_components"]),
                    source_ref=str(item_row["source_ref"]) if item_row["source_ref"] is not None else None,
                    source_label=str(item_row["source_label"]) if item_row["source_label"] is not None else None,
                    processing_status=RecommendationProcessingStatus(str(item_row["processing_status"]))
                    if item_row["processing_status"] is not None
                    else None,
                    processed_at=item_row["processed_at"],
                    id=str(item_row["id"]),
                )
            )
        sessions = [
            RecommendationSession(
                strategy=str(row["strategy"]),
                items=items_by_session.get(str(row["id"]), []),
                id=str(row["id"]),
                created_at=row["created_at"],
                debug_metadata=self._dict_from_json_value(row["context_snapshot"]),
            )
            for row in session_rows
        ]
        self.sessions.update({session.id: session for session in sessions})
        return sessions

    def recent_recommendation_movie_ids(self, session_count: int) -> set[str]:
        if session_count <= 0:
            return set()
        rows = self.connection.execute(
            """
            SELECT ri.movie_id
            FROM recommendation_items ri
            JOIN (
                SELECT id
                FROM recommendation_sessions
                ORDER BY created_at DESC
                LIMIT %s
            ) recent_sessions ON recent_sessions.id = ri.session_id
            """,
            (session_count,),
        ).fetchall()
        return {str(row["movie_id"]) for row in rows}

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

    def mark_recommendation_item_processed(
        self,
        session_id: str,
        item_id: str,
        status: RecommendationProcessingStatus,
    ) -> RecommendationItem:
        processed_at = datetime.now(timezone.utc)
        self.connection.execute(
            """
            UPDATE recommendation_items
            SET processing_status = %s, processed_at = %s
            WHERE session_id = %s AND id = %s
            """,
            (status.value, processed_at, session_id, item_id),
        )
        session = self.get_session(session_id)
        if session is None:
            raise KeyError("recommendation session not found")
        item = next(candidate for candidate in session.items if candidate.id == item_id)
        item.processing_status = status
        item.processed_at = processed_at
        return item

    def clear_recommendation_item_processed(self, session_id: str, item_id: str) -> RecommendationItem:
        self.connection.execute(
            """
            UPDATE recommendation_items
            SET processing_status = NULL, processed_at = NULL
            WHERE session_id = %s AND id = %s
            """,
            (session_id, item_id),
        )
        session = self.get_session(session_id)
        if session is None:
            raise KeyError("recommendation session not found")
        item = next(candidate for candidate in session.items if candidate.id == item_id)
        item.processing_status = None
        item.processed_at = None
        return item

    def delete_latest_feedback(self, session_id: str, item_id: str, feedback_type: FeedbackType) -> Feedback | None:
        row = self.connection.execute(
            """
            SELECT id
            FROM feedback
            WHERE session_id = %s AND item_id = %s AND feedback_type = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (session_id, item_id, feedback_type.value),
        ).fetchone()
        if row is None:
            return None
        feedback_id = str(row["id"])
        self.connection.execute("DELETE FROM feedback WHERE id = %s", (feedback_id,))
        deleted = next((feedback for feedback in self.feedback if feedback.id == feedback_id), None)
        self.feedback = [feedback for feedback in self.feedback if feedback.id != feedback_id]
        return deleted

    def deactivate_candidate_pool_movie(self, movie_id: str) -> None:
        self.connection.execute(
            """
            UPDATE candidate_pool
            SET active = FALSE, updated_at = %s
            WHERE movie_id = %s AND active = TRUE
            """,
            (datetime.now(timezone.utc), movie_id),
        )
        self._active_candidate_movie_ids.discard(movie_id)

    def restore_candidate_pool_movie_if_eligible(self, movie_id: str) -> None:
        cursor = self.connection.execute(
            """
            UPDATE candidate_pool cp
            SET active = TRUE, updated_at = %s
            WHERE cp.movie_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM viewing_history vh
                  WHERE vh.movie_id = cp.movie_id AND vh.deleted_at IS NULL
              )
              AND NOT EXISTS (
                  SELECT 1 FROM wishlist w
                  WHERE w.movie_id = cp.movie_id AND w.status = %s
              )
            """,
            (datetime.now(timezone.utc), movie_id, WishlistStatus.ACTIVE.value),
        )
        if cursor.rowcount:
            self._active_candidate_movie_ids.add(movie_id)

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
        return sorted(
            [item for item in self.wishlist.values() if item.status == WishlistStatus.ACTIVE],
            key=lambda item: item.created_at,
            reverse=True,
        )

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

    def remove_wishlist_item(self, wishlist_item: WishlistItem) -> WishlistItem:
        wishlist_item.status = WishlistStatus.REMOVED
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
        subject_id = _subject_id_from_douban_url(self.movies_by_id[history.movie_id].douban_url)
        if not subject_id:
            raise ValueError("movie has no Douban subject id")
        self.connection.execute(
            """
            INSERT INTO viewing_history (
                id, movie_id, douban_subject_id, watched_date, user_rating, quality, comment,
                source_row_checksum, source_sheet_name, source_row_number, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                movie_id = excluded.movie_id,
                douban_subject_id = excluded.douban_subject_id,
                watched_date = excluded.watched_date,
                user_rating = excluded.user_rating,
                quality = excluded.quality,
                comment = excluded.comment,
                source_row_checksum = excluded.source_row_checksum,
                updated_at = excluded.updated_at
            """,
            (
                history.id,
                history.movie_id,
                subject_id,
                history.watched_date,
                history.user_rating,
                history.quality,
                history.comment,
                f"wishlist:{wishlist_id}",
                f"wishlist:{wishlist_id}",
                now,
                now,
            ),
        )
        self.history.append(history)
        return history

    def find_recommendation_item_by_session_and_movie(
        self,
        session_id: str,
        movie_id: str,
    ) -> RecommendationItem | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        return next((item for item in session.items if item.movie.id == movie_id), None)

    def list_current_not_interested(self) -> list[NotInterestedItem]:
        self.refresh()
        latest_by_movie = self._latest_state_feedback_by_movie()
        items = [
            NotInterestedItem(
                movie=self.movies_by_id[feedback.movie_id],
                state_event_id=feedback.id,
                state_changed_at=feedback.created_at,
                session_id=feedback.session_id,
                item_id=feedback.item_id,
            )
            for feedback in latest_by_movie.values()
            if feedback.feedback_type == FeedbackType.NOT_INTERESTED and feedback.movie_id in self.movies_by_id
        ]
        return sorted(items, key=lambda item: item.state_changed_at, reverse=True)

    def _current_feedback_state(self, movie_id: str) -> FeedbackType | None:
        latest = self._latest_state_feedback_by_movie().get(movie_id)
        return latest.feedback_type if latest else None

    def _latest_state_feedback_by_movie(self) -> dict[str, Feedback]:
        state_events = {
            FeedbackType.WANT_TO_WATCH,
            FeedbackType.MAYBE_LATER,
            FeedbackType.NOT_INTERESTED,
            FeedbackType.REMOVED_FROM_WISHLIST,
            FeedbackType.CLEAR_NOT_INTERESTED,
        }
        latest: dict[str, Feedback] = {}
        for item in self.feedback:
            if item.feedback_type in state_events:
                latest[item.movie_id] = item
        return latest

    def _candidate_from_movie(self, movie: Movie) -> RecommendationCandidate:
        source_ref, source_label = self._active_candidate_sources.get(movie.id, (None, None))
        return RecommendationCandidate(movie=movie, source_ref=source_ref, source_label=source_label)

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
            poster_url=row.get("poster_url"),
        )

    def _tuple_from_json_value(self, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = json.loads(value)
        return tuple(str(item) for item in value)

    def _dict_from_json_value(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, str):
            value = json.loads(value)
        return dict(value)

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
                    genres, countries, douban_rating, douban_vote_count, poster_url
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

    def _jsonb(self, value: Any):
        from psycopg.types.json import Jsonb

        return Jsonb(value)


class RecommendationService:
    def __init__(self, repository: MovieRepository, explore_pool_size: int = 10) -> None:
        self.repository = repository
        self.explore_pool_size = explore_pool_size

    def recommend(
        self,
        strategy: Strategy = "hybrid",
        explore_seed: int | None = None,
        exposure_cooldown_sessions: int = 5,
    ) -> RecommendationSession:
        with self._repository_lock_context():
            return self._recommend(
                strategy=strategy,
                explore_seed=explore_seed,
                exposure_cooldown_sessions=exposure_cooldown_sessions,
            )

    def _recommend(
        self,
        strategy: Strategy = "hybrid",
        explore_seed: int | None = None,
        exposure_cooldown_sessions: int = 5,
    ) -> RecommendationSession:
        candidates = self.repository.active_candidates()
        if len(candidates) < RECOMMENDATION_SESSION_SIZE:
            raise ValueError(f"At least {RECOMMENDATION_SESSION_SIZE} eligible candidates are required")

        requested_cooldown = max(0, exposure_cooldown_sessions)
        applied_cooldown, candidates = self._apply_exposure_cooldown(candidates, requested_cooldown)
        scoring_strategy: Literal["popularity", "content", "hybrid"] = "hybrid" if strategy == "bandit_hybrid" else strategy
        content_profile = (
            build_content_profile(self.repository.history, self.repository.movies_by_id)
            if scoring_strategy in {"content", "hybrid"}
            else None
        )

        scored = [
            (candidate, self._score_with_feedback_penalty(candidate.movie, scoring_strategy, content_profile))
            for candidate in candidates
        ]
        scored.sort(key=lambda item: item[1]["total"], reverse=True)

        exploit_candidates = [candidate for candidate, _ in scored[:EXPLOIT_SLOT_COUNT]]
        selected = list(exploit_candidates)
        remaining = [(candidate, scores) for candidate, scores in scored if candidate not in selected]
        selected_scores_by_movie_id = {candidate.movie.id: scores for candidate, scores in scored}
        bandit_metadata: dict[str, Any] = {}
        if strategy == "bandit_hybrid":
            explore_candidates, bandit_scores_by_movie_id, bandit_metadata = self._select_bandit_explore(
                remaining,
                selected,
                limit=EXPLORE_SLOT_COUNT,
                explore_seed=explore_seed,
            )
            selected_scores_by_movie_id.update(bandit_scores_by_movie_id)
        else:
            explore_candidates = self._select_explore(
                remaining,
                selected,
                limit=EXPLORE_SLOT_COUNT,
                explore_seed=explore_seed,
            )
        selected.extend(explore_candidates)

        items = [
            RecommendationItem(
                movie=candidate.movie,
                rank=index + 1,
                slot_type=SlotType.EXPLOIT if index < EXPLOIT_SLOT_COUNT else SlotType.EXPLORE,
                score=selected_scores_by_movie_id[candidate.movie.id]["total"],
                score_components=selected_scores_by_movie_id[candidate.movie.id],
                source_ref=candidate.source_ref,
                source_label=self._source_label(candidate),
            )
            for index, candidate in enumerate(selected)
        ]
        return self.repository.save_session(
            RecommendationSession(
                strategy=strategy,
                items=items,
                debug_metadata={
                    "requested_exposure_cooldown_sessions": requested_cooldown,
                    "applied_exposure_cooldown_sessions": applied_cooldown,
                    "cooldown_relaxed": applied_cooldown < requested_cooldown,
                    "seed": explore_seed,
                    "eligible_candidate_count": len(candidates),
                    **bandit_metadata,
                },
            )
        )

    def _repository_lock_context(self):
        lock = getattr(self.repository, "lock", None)
        return lock if lock is not None else nullcontext()

    def get_session(self, session_id: str) -> RecommendationSession:
        session = self.repository.get_session(session_id)
        if session is None:
            raise KeyError("recommendation session not found")
        return session

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
            self.repository.mark_recommendation_item_processed(
                session_id,
                item_id,
                RecommendationProcessingStatus.ADDED_TO_WISHLIST,
            )
        elif request.feedback_type == FeedbackType.NOT_INTERESTED:
            self.repository.mark_recommendation_item_processed(
                session_id,
                item_id,
                RecommendationProcessingStatus.NOT_INTERESTED,
            )
            self.repository.deactivate_candidate_pool_movie(item.movie.id)
        elif request.feedback_type == FeedbackType.MAYBE_LATER:
            self.repository.mark_recommendation_item_processed(
                session_id,
                item_id,
                RecommendationProcessingStatus.MAYBE_LATER,
            )
        elif request.feedback_type == FeedbackType.CLEAR_NOT_INTERESTED:
            self.repository.restore_candidate_pool_movie_if_eligible(item.movie.id)
        return feedback

    def mark_watched_from_recommendation(
        self,
        session_id: str,
        item_id: str,
        movie_id: str | None = None,
    ) -> RecommendationItem:
        session = self.repository.get_session(session_id)
        if session is None:
            raise KeyError("recommendation session not found")
        item = next((candidate for candidate in session.items if candidate.id == item_id), None)
        if item is None:
            raise KeyError("recommendation item not found")
        if movie_id is not None and item.movie.id != movie_id:
            raise ValueError("recommendation item does not match recorded movie")
        processed = self.repository.mark_recommendation_item_processed(
            session_id,
            item_id,
            RecommendationProcessingStatus.WATCHED,
        )
        self.repository.deactivate_candidate_pool_movie(item.movie.id)
        return processed

    def undo_recommendation_item_processing(self, session_id: str, item_id: str) -> RecommendationItem:
        session = self.repository.get_session(session_id)
        if session is None:
            raise KeyError("recommendation session not found")
        item = next((candidate for candidate in session.items if candidate.id == item_id), None)
        if item is None:
            raise KeyError("recommendation item not found")

        previous_status = item.processing_status
        if previous_status == RecommendationProcessingStatus.ADDED_TO_WISHLIST:
            wishlist_item = self.repository.find_active_wishlist_by_movie(item.movie.id)
            if wishlist_item is not None:
                self.repository.remove_wishlist_item(wishlist_item)
            self.repository.delete_latest_feedback(session_id, item_id, FeedbackType.WANT_TO_WATCH)
            self.repository.restore_candidate_pool_movie_if_eligible(item.movie.id)
        elif previous_status == RecommendationProcessingStatus.NOT_INTERESTED:
            self.repository.delete_latest_feedback(session_id, item_id, FeedbackType.NOT_INTERESTED)
            self.repository.restore_candidate_pool_movie_if_eligible(item.movie.id)
        elif previous_status == RecommendationProcessingStatus.MAYBE_LATER:
            self.repository.delete_latest_feedback(session_id, item_id, FeedbackType.MAYBE_LATER)
        elif previous_status == RecommendationProcessingStatus.WATCHED:
            self.repository.restore_candidate_pool_movie_if_eligible(item.movie.id)

        return self.repository.clear_recommendation_item_processed(session_id, item_id)

    def mark_watched_movie(self, movie_id: str) -> None:
        wishlist_item = self.repository.find_active_wishlist_by_movie(movie_id)
        if wishlist_item is not None:
            self.repository.mark_wishlist_watched(wishlist_item)
        self.repository.deactivate_candidate_pool_movie(movie_id)

    def restore_candidate_pool_movie_if_eligible(self, movie_id: str) -> None:
        with self._repository_lock_context():
            self.repository.restore_candidate_pool_movie_if_eligible(movie_id)

    def mark_wishlist_item_watched_from_record(self, wishlist_id: str, movie_id: str | None = None) -> WishlistItem:
        wishlist_item = self.repository.find_wishlist_item(wishlist_id)
        if wishlist_item is None:
            raise KeyError("wishlist item not found")
        if wishlist_item.status != WishlistStatus.ACTIVE:
            raise ValueError("wishlist item is not active")
        if movie_id is not None and wishlist_item.movie.id != movie_id:
            raise ValueError("wishlist item does not match recorded movie")
        wishlist_item = self.repository.mark_wishlist_watched(wishlist_item)
        self.repository.deactivate_candidate_pool_movie(wishlist_item.movie.id)
        return wishlist_item

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
        history = self.repository.add_viewing_history(history, wishlist_id)
        self.repository.deactivate_candidate_pool_movie(wishlist_item.movie.id)
        return history

    def remove_from_wishlist(self, wishlist_id: str) -> WishlistItem:
        wishlist_item = self.repository.find_wishlist_item(wishlist_id)
        if wishlist_item is None:
            raise KeyError("wishlist item not found")
        if wishlist_item.status != WishlistStatus.ACTIVE:
            raise ValueError("wishlist item is not active")

        removed = self.repository.remove_wishlist_item(wishlist_item)
        self.repository.add_feedback(
            Feedback(
                session_id=removed.source_session_id,
                item_id=self._feedback_item_id_for_movie(removed.source_session_id, removed.movie.id),
                movie_id=removed.movie.id,
                feedback_type=FeedbackType.REMOVED_FROM_WISHLIST,
                feedback_value=self._feedback_value(FeedbackType.REMOVED_FROM_WISHLIST),
            )
        )
        return removed

    def clear_not_interested(self, movie_id: str) -> NotInterestedItem:
        current = next((item for item in self.repository.list_current_not_interested() if item.movie.id == movie_id), None)
        if current is None:
            raise KeyError("not interested movie not found")
        self.repository.add_feedback(
            Feedback(
                session_id=current.session_id,
                item_id=current.item_id,
                movie_id=current.movie.id,
                feedback_type=FeedbackType.CLEAR_NOT_INTERESTED,
                feedback_value=self._feedback_value(FeedbackType.CLEAR_NOT_INTERESTED),
            )
        )
        self.repository.restore_candidate_pool_movie_if_eligible(current.movie.id)
        return current

    def _feedback_item_id_for_movie(self, session_id: str, movie_id: str) -> str:
        session = self.repository.get_session(session_id)
        if session is None:
            raise KeyError("recommendation session not found")
        item = next((candidate for candidate in session.items if candidate.movie.id == movie_id), None)
        if item is None:
            raise KeyError("recommendation item not found")
        return item.id

    def _apply_exposure_cooldown(
        self,
        candidates: list[RecommendationCandidate],
        requested_cooldown: int,
    ) -> tuple[int, list[RecommendationCandidate]]:
        applied_cooldown = requested_cooldown
        while applied_cooldown > 0:
            exposed_movie_ids = self.repository.recent_recommendation_movie_ids(applied_cooldown)
            filtered = [candidate for candidate in candidates if candidate.movie.id not in exposed_movie_ids]
            if len(filtered) >= RECOMMENDATION_SESSION_SIZE:
                return applied_cooldown, filtered
            applied_cooldown -= 1
        return 0, candidates

    def _score(
        self,
        movie: Movie,
        strategy: Strategy,
        content_profile: ContentProfile | None = None,
    ) -> dict[str, float]:
        if strategy == "popularity":
            total = popularity_score(movie)
            return {"public_quality": total, "total": total}
        if strategy == "content":
            total = (
                content_score_from_profile(movie, content_profile)
                if content_profile is not None
                else content_score(movie, self.repository.history, self.repository.movies_by_id)
            )
            return {"personal_preference": total, "total": total}
        if strategy == "hybrid":
            return hybrid_score(
                movie,
                self.repository.history,
                self.repository.movies_by_id,
                content_profile=content_profile,
            )
        raise ValueError(f"Unknown recommendation strategy: {strategy}")

    def _score_with_feedback_penalty(
        self,
        movie: Movie,
        strategy: Strategy,
        content_profile: ContentProfile | None = None,
    ) -> dict[str, float]:
        scores = dict(self._score(movie, strategy, content_profile))
        penalty = self._maybe_later_penalty(movie.id)
        if penalty:
            scores["maybe_later_penalty"] = -penalty
            scores["total"] = scores["total"] - penalty
        return scores

    def _maybe_later_penalty(self, movie_id: str) -> float:
        now = datetime.now(timezone.utc)
        count = sum(
            1
            for feedback in self.repository.feedback
            if feedback.movie_id == movie_id
            and feedback.feedback_type == FeedbackType.MAYBE_LATER
            and now - _as_aware(feedback.created_at) <= MAYBE_LATER_DOWNRANKING_WINDOW
        )
        return min(6.0, count * 1.5)

    def _select_explore(
        self,
        scored: list[tuple[RecommendationCandidate, dict[str, float]]],
        selected: list[RecommendationCandidate],
        limit: int,
        explore_seed: int | None = None,
    ) -> list[RecommendationCandidate]:
        rng = random.Random(explore_seed) if explore_seed is not None else random.Random()
        explore: list[RecommendationCandidate] = []
        for _ in range(limit):
            if not scored:
                break
            selected_movies = [candidate.movie for candidate in selected + explore]
            ranked = sorted(
                (
                    (
                        candidate,
                        scores,
                        diversity_gain(candidate.movie, selected_movies) * 0.65 + scores["total"] * 0.35,
                    )
                    for candidate, scores in scored
                ),
                key=lambda item: item[2],
                reverse=True,
            )
            sample_pool = ranked[: max(limit, self.explore_pool_size)]
            weights = _positive_weights([item[2] for item in sample_pool])
            candidate = rng.choices([item[0] for item in sample_pool], weights=weights, k=1)[0]
            scored = [(item, scores) for item, scores in scored if item.movie.id != candidate.movie.id]
            explore.append(candidate)
        return explore

    def _select_bandit_explore(
        self,
        scored: list[tuple[RecommendationCandidate, dict[str, Any]]],
        selected: list[RecommendationCandidate],
        limit: int,
        explore_seed: int | None = None,
    ) -> tuple[list[RecommendationCandidate], dict[str, dict[str, Any]], dict[str, Any]]:
        metadata: dict[str, Any] = {
            "feature_version": FEATURE_VERSION,
            "reward_version": REWARD_VERSION,
            "bandit_min_examples": BANDIT_MIN_EXAMPLES,
            "bandit_used": False,
        }
        try:
            examples = build_bandit_training_examples(
                sessions=self.repository.recommendation_training_sessions(),
                feedback=self.repository.feedback,
                history=self.repository.history,
                movies_by_id=self.repository.movies_by_id,
                wishlist=self.repository.wishlist.values(),
            )
            model = fit_diagonal_linear_thompson_model(examples)
            try:
                write_latest_model_cache(model)
            except OSError:
                pass
            metadata["trainable_example_count"] = model.trained_example_count
            if not should_use_bandit_explore(model):
                metadata["bandit_fallback_reason"] = "insufficient_training_examples"
                return (
                    self._select_explore(scored, selected, limit=limit, explore_seed=explore_seed),
                    {},
                    metadata,
                )

            context = build_bandit_feature_context(
                history=self.repository.history,
                movies_by_id=self.repository.movies_by_id,
                wishlist=self.repository.wishlist.values(),
                feedback=self.repository.feedback,
            )
            rng = random.Random(explore_seed) if explore_seed is not None else random.Random()
            bandit_scored = []
            for candidate, scores in scored:
                features = build_bandit_feature_vector(
                    candidate.movie,
                    scores,
                    context,
                    source_ref=candidate.source_ref,
                )
                bandit_score = model.sampled_score(features, rng)
                score_components = {
                    **scores,
                    "bandit_sample": bandit_score.sample,
                    "bandit_mean": bandit_score.mean,
                    "bandit_uncertainty": bandit_score.uncertainty,
                    "feature_version": FEATURE_VERSION,
                }
                bandit_scored.append((candidate, score_components, bandit_score.sample))

            explore: list[RecommendationCandidate] = []
            score_components_by_movie_id: dict[str, dict[str, Any]] = {}
            remaining = list(bandit_scored)
            for _ in range(limit):
                if not remaining:
                    break
                selected_movies = [candidate.movie for candidate in selected + explore]
                candidate, score_components, _ = max(
                    remaining,
                    key=lambda item: item[2] + diversity_gain(item[0].movie, selected_movies) * 0.10,
                )
                explore.append(candidate)
                score_components_by_movie_id[candidate.movie.id] = score_components
                remaining = [item for item in remaining if item[0].movie.id != candidate.movie.id]

            metadata["bandit_used"] = True
            return explore, score_components_by_movie_id, metadata
        except Exception:
            metadata.setdefault("trainable_example_count", 0)
            metadata["bandit_fallback_reason"] = "training_failed"
            return (
                self._select_explore(scored, selected, limit=limit, explore_seed=explore_seed),
                {},
                metadata,
            )

    def _feedback_value(self, feedback_type: FeedbackType) -> float:
        return {
            FeedbackType.WANT_TO_WATCH: 0.7,
            FeedbackType.MAYBE_LATER: 0.2,
            FeedbackType.NOT_INTERESTED: -0.8,
            FeedbackType.OPENED_DOUBAN: 0.1,
            FeedbackType.REMOVED_FROM_WISHLIST: 0.2,
            FeedbackType.CLEAR_NOT_INTERESTED: 0.0,
        }[feedback_type]

    def to_session_response(self, session: RecommendationSession) -> dict:
        return {
            "id": session.id,
            "strategy": session.strategy,
            "created_at": session.created_at.isoformat(),
            "debug_metadata": session.debug_metadata,
            "items": [self.to_recommendation_item_response(item) for item in session.items],
        }

    def to_recommendation_item_response(self, item: RecommendationItem) -> dict:
        return {
            "id": item.id,
            "rank": item.rank,
            "slot_type": item.slot_type.value,
            "score": item.score,
            "score_components": item.score_components,
            "source_ref": item.source_ref,
            "source_label": self._source_label_for_item(item),
            "processing_status": item.processing_status.value if item.processing_status else None,
            "processed_at": item.processed_at.isoformat() if item.processed_at else None,
            "movie": self._movie_response(item.movie),
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

    def to_wishlist_response(self, limit: int = 10, offset: int = 0) -> dict:
        items = self.repository.list_active_wishlist()
        paged_items = items[offset : offset + limit]
        return {
            "limit": limit,
            "offset": offset,
            "total": len(items),
            "items": [self.to_wishlist_item_response(item) for item in paged_items],
        }

    def to_wishlist_item_response(self, item: WishlistItem) -> dict:
        recommendation_item = self.repository.find_recommendation_item_by_session_and_movie(
            item.source_session_id,
            item.movie.id,
        )
        return {
            "id": item.id,
            "status": item.status.value,
            "source_session_id": item.source_session_id,
            "score": recommendation_item.score if recommendation_item else None,
            "source_ref": recommendation_item.source_ref if recommendation_item else None,
            "source_label": self._source_label_for_item(recommendation_item) if recommendation_item else None,
            "created_at": item.created_at.isoformat(),
            "closed_at": item.closed_at.isoformat() if item.closed_at else None,
            "movie": self._movie_response(item.movie),
        }

    def to_not_interested_response(self, limit: int = 10, offset: int = 0) -> dict:
        items = self.repository.list_current_not_interested()
        paged_items = items[offset : offset + limit]
        return {
            "limit": limit,
            "offset": offset,
            "total": len(items),
            "items": [self.to_not_interested_item_response(item) for item in paged_items],
        }

    def to_not_interested_item_response(self, item: NotInterestedItem) -> dict:
        return {
            "id": item.state_event_id,
            "movie_id": item.movie.id,
            "state": FeedbackType.NOT_INTERESTED.value,
            "state_changed_at": item.state_changed_at.isoformat(),
            "session_id": item.session_id,
            "item_id": item.item_id,
            "movie": self._movie_response(item.movie),
        }

    def to_viewing_history_response(self, history: ViewingHistory) -> dict:
        response = asdict(history)
        response["watched_date"] = history.watched_date.isoformat()
        response["created_at"] = history.created_at.isoformat()
        return response

    def _movie_response(self, movie: Movie) -> dict:
        directors = display_person_names(movie.directors)
        cast = display_person_names(movie.actors)
        return {
            "id": movie.id,
            "title": movie.title,
            "year": movie.year,
            "director": ", ".join(directors),
            "directors": directors,
            "main_cast": cast[:3],
            "cast": cast,
            "douban_rating": movie.douban_rating,
            "awards": list(movie.awards),
            "douban_url": movie.douban_url,
            "poster_url": movie.poster_url,
        }

    def _source_label(self, candidate: RecommendationCandidate) -> str | None:
        if candidate.source_ref and candidate.source_ref.startswith("top"):
            return candidate.source_ref
        if candidate.source_ref and candidate.source_ref.startswith("recommended_from:"):
            if candidate.source_label and candidate.source_label != "Recommend from unknown movie":
                prefix = "recommended from "
                if candidate.source_label.lower().startswith(prefix):
                    return f"Recommend from {candidate.source_label[len(prefix):]}"
                return candidate.source_label
            source_title = self._source_movie_title(candidate.source_ref)
            if source_title:
                return f"Recommend from {source_title}"
            return "Recommend from unknown movie"
        return candidate.source_label

    def _source_label_for_item(self, item: RecommendationItem) -> str | None:
        return self._source_label(
            RecommendationCandidate(
                movie=item.movie,
                source_ref=item.source_ref,
                source_label=item.source_label,
            )
        )

    def _source_movie_title(self, source_ref: str | None) -> str | None:
        if not source_ref or not source_ref.startswith("recommended_from:"):
            return None
        subject_id = source_ref.split(":", 1)[1]
        for movie in self.repository.movies_by_id.values():
            if _subject_id_from_douban_url(movie.douban_url) == subject_id:
                return movie.title
        return None


def _subject_id_from_douban_url(url: str) -> str | None:
    parts = url.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] == "subject":
        return parts[-1]
    return None


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


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
