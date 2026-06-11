from datetime import date
import unittest

from backend.app.models.domain import Movie, ViewingHistory
from backend.app.recommenders.simple import (
    build_content_profile,
    content_score,
    content_score_from_profile,
    hybrid_score,
)


class SimpleRecommenderTest(unittest.TestCase):
    def test_precomputed_profile_preserves_content_and_hybrid_scores(self) -> None:
        liked = _movie("liked", ("Drama",), ("Japan",), ("Director A",), ("Actor A",))
        disliked = _movie("disliked", ("Horror",), ("USA",), ("Director B",), ("Actor B",))
        candidate = _movie("candidate", ("Drama", "Horror"), ("Japan",), ("Director A",), ("Actor C",))
        movies_by_id = {movie.id: movie for movie in (liked, disliked, candidate)}
        history = [
            ViewingHistory(movie_id=liked.id, watched_date=date(2024, 1, 1), user_rating=4.5),
            ViewingHistory(movie_id=disliked.id, watched_date=date(2024, 1, 2), user_rating=2.0),
        ]

        profile = build_content_profile(history, movies_by_id)

        self.assertEqual(
            content_score(candidate, history, movies_by_id),
            content_score_from_profile(candidate, profile),
        )
        self.assertEqual(
            hybrid_score(candidate, history, movies_by_id),
            hybrid_score(candidate, history, movies_by_id, content_profile=profile),
        )


def _movie(
    movie_id: str,
    genres: tuple[str, ...],
    countries: tuple[str, ...],
    directors: tuple[str, ...],
    actors: tuple[str, ...],
) -> Movie:
    return Movie(
        id=movie_id,
        title=movie_id,
        year=2000,
        directors=directors,
        actors=actors,
        genres=genres,
        countries=countries,
        douban_rating=8.0,
        douban_vote_count=1000,
        douban_url=f"https://movie.douban.com/subject/{movie_id}/",
    )


if __name__ == "__main__":
    unittest.main()
