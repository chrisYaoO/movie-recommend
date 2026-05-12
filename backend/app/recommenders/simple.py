from __future__ import annotations

import math
from collections import Counter

from backend.app.models.domain import Movie, ViewingHistory


def popularity_score(movie: Movie) -> float:
    vote_factor = math.log10(max(movie.douban_vote_count, 1))
    return movie.douban_rating * 0.75 + vote_factor * 0.25


def content_score(movie: Movie, history: list[ViewingHistory], movies_by_id: dict[str, Movie]) -> float:
    positive_profile: Counter[str] = Counter()
    negative_profile: Counter[str] = Counter()

    for watched in history:
        source = movies_by_id.get(watched.movie_id)
        if source is None or watched.user_rating is None:
            continue
        features = _features(source)
        if watched.user_rating >= 4.0:
            weight = 1.0 if watched.user_rating >= 4.5 else 0.6
            positive_profile.update({feature: weight for feature in features})
        else:
            negative_profile.update({feature: 1.0 for feature in features})

    features = _features(movie)
    positive = sum(positive_profile[feature] for feature in features)
    negative = sum(negative_profile[feature] for feature in features)
    normalizer = max(len(features), 1)
    return (positive - negative) / normalizer


def hybrid_score(movie: Movie, history: list[ViewingHistory], movies_by_id: dict[str, Movie]) -> dict[str, float]:
    personal = content_score(movie, history, movies_by_id)
    public = popularity_score(movie)
    novelty = 0.3 if movie.douban_vote_count < 100000 else 0.0
    total = personal * 0.45 + public * 0.45 + novelty * 0.10
    return {
        "personal_preference": personal,
        "public_quality": public,
        "novelty": novelty,
        "total": total,
    }


def diversity_gain(movie: Movie, selected: list[Movie]) -> float:
    if not selected:
        return 1.0

    selected_genres = {genre for item in selected for genre in item.genres}
    selected_countries = {country for item in selected for country in item.countries}
    selected_decades = {item.decade for item in selected}

    genre_gain = len(set(movie.genres) - selected_genres) / max(len(movie.genres), 1)
    country_gain = len(set(movie.countries) - selected_countries) / max(len(movie.countries), 1)
    decade_gain = 1.0 if movie.decade not in selected_decades else 0.0
    return genre_gain * 0.45 + country_gain * 0.35 + decade_gain * 0.20


def _features(movie: Movie) -> set[str]:
    features = {f"genre:{value}" for value in movie.genres}
    features.update(f"country:{value}" for value in movie.countries)
    features.update(f"director:{value}" for value in movie.directors)
    features.update(f"actor:{value}" for value in movie.actors[:3])
    features.add(f"decade:{movie.decade}")
    return features
