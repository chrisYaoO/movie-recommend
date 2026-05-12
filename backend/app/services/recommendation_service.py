from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Literal

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

    def _has_hard_negative(self, movie_id: str) -> bool:
        return any(item.movie_id == movie_id and item.feedback_type == FeedbackType.NOT_INTERESTED for item in self.feedback)


class RecommendationService:
    def __init__(self, repository: InMemoryMovieRepository) -> None:
        self.repository = repository

    def recommend(self, strategy: Strategy = "hybrid") -> RecommendationSession:
        candidates = self.repository.active_candidates()
        if len(candidates) < 5:
            raise ValueError("At least five eligible candidates are required")

        scored = [(movie, self._score(movie, strategy)) for movie in candidates]
        scored.sort(key=lambda item: item[1]["total"], reverse=True)

        exploit_movies = [movie for movie, _ in scored[:3]]
        selected = list(exploit_movies)
        remaining = [(movie, scores) for movie, scores in scored if movie not in selected]
        explore_movies = self._select_explore(remaining, selected, limit=2)
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
        session = self.repository.sessions.get(session_id)
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
        wishlist_item = self.repository.wishlist.get(wishlist_id)
        if wishlist_item is None:
            raise KeyError("wishlist item not found")
        if wishlist_item.status != WishlistStatus.ACTIVE:
            raise ValueError("wishlist item is not active")

        wishlist_item.status = WishlistStatus.WATCHED
        wishlist_item.closed_at = datetime.now(timezone.utc)
        history = ViewingHistory(
            movie_id=wishlist_item.movie.id,
            watched_date=request.watched_date,
            user_rating=request.user_rating,
            quality=request.quality,
            comment=request.comment,
        )
        self.repository.history.append(history)
        return history

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

    def _select_explore(self, scored: list[tuple[Movie, dict[str, float]]], selected: list[Movie], limit: int) -> list[Movie]:
        explore: list[Movie] = []
        for _ in range(limit):
            if not scored:
                break
            scored.sort(
                key=lambda item: diversity_gain(item[0], selected + explore) * 0.65 + item[1]["total"] * 0.35,
                reverse=True,
            )
            movie, _ = scored.pop(0)
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
                for item in self.repository.wishlist.values()
                if item.status == WishlistStatus.ACTIVE
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


service = RecommendationService(InMemoryMovieRepository())
