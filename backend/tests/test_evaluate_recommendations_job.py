import unittest

from backend.app.models.domain import RecommendationSession
from jobs.evaluate_recommendations import (
    CandidatePoolHealthSummary,
    RecommendationEvaluationItem,
    RecommendationEvaluationResult,
    _run_seed,
    _summarize,
    render_text,
)


class EvaluateRecommendationsJobTest(unittest.TestCase):
    def test_summarizes_recommendation_quality_metrics(self) -> None:
        items = (
            _item(1, 1, "exploit", "m-1", "Movie One", watched=False, sources=("douban_top250:top1",)),
            _item(1, 2, "explore", "m-2", "Movie Two", watched=True, sources=("douban_recommendation:recommended_from:x",)),
            _item(2, 1, "exploit", "m-1", "Movie One", watched=False, sources=("douban_top250:top1",)),
            _item(2, 2, "explore", "m-1", "Movie One", watched=False, sources=("douban_top250:top1",)),
        )

        summary = _summarize(
            items,
            runs=2,
            historical_rewards_by_movie_id={"m-2": 0.10, "m-1": -1.0},
            current_negative_movie_ids={"m-1"},
        )

        self.assertEqual(4, summary.total_items)
        self.assertEqual(2, summary.unique_movies)
        self.assertEqual(1, summary.duplicate_in_session_count)
        self.assertEqual(1, summary.watched_leak_count)
        self.assertEqual(3, summary.negative_feedback_recurrence_count)
        self.assertEqual(2, summary.explore_slot_reward_count)
        self.assertEqual(1, summary.explore_slot_positive_reward_count)
        self.assertEqual(0.5, summary.explore_slot_reward_rate)
        self.assertEqual({"exploit": 2, "explore": 2}, summary.slot_mix)
        self.assertEqual({"douban_top250": 3, "douban_recommendation": 1}, summary.source_mix)
        self.assertEqual({"Movie One": 3}, summary.repeated_movies)

    def test_renders_text_report(self) -> None:
        items = (_item(1, 1, "exploit", "m-1", "Movie One"),)
        result = RecommendationEvaluationResult(
            strategy="hybrid",
            items=items,
            summary=_summarize(items, runs=1),
            pool_health=CandidatePoolHealthSummary(
                active_pool_entries=10,
                active_unique_movies=9,
                eligible_unique_movies=8,
                watched_candidate_count=1,
                active_wishlist_candidate_count=1,
                not_interested_candidate_count=0,
                average_douban_rating=8.1,
                queue_status_counts={"pending": 5, "enriched": 10},
                active_source_mix={"douban_top250": 3, "douban_recommendation": 7},
                metadata_missing_counts={"genres": 1},
            ),
        )

        report = render_text(result)

        self.assertIn("run 1", report)
        self.assertIn("Movie One", report)
        self.assertIn("candidate_pool_health", report)
        self.assertIn("eligible_unique_movies=8", report)
        self.assertIn("active_source_mix:", report)
        self.assertIn("summary", report)
        self.assertIn("strategy=hybrid", report)
        self.assertIn("negative_feedback_recurrence_count=0", report)
        self.assertIn("explore_slot_reward_rate=n/a", report)

    def test_summarizes_and_renders_bandit_metadata(self) -> None:
        items = (_item(1, 1, "explore", "m-1", "Movie One"),)
        sessions = (
            RecommendationSession(
                strategy="bandit_hybrid",
                items=[],
                debug_metadata={
                    "trainable_example_count": 19,
                    "bandit_used": False,
                    "bandit_fallback_reason": "insufficient_training_examples",
                },
            ),
        )

        summary = _summarize(items, runs=1, sessions=sessions)
        report = render_text(
            RecommendationEvaluationResult(
                strategy="bandit_hybrid",
                items=items,
                summary=summary,
            )
        )

        self.assertEqual(19, summary.bandit_trainable_example_count)
        self.assertEqual(0, summary.bandit_used_count)
        self.assertEqual(1, summary.bandit_fallback_count)
        self.assertEqual({"insufficient_training_examples": 1}, summary.bandit_fallback_reasons)
        self.assertIn("bandit:", report)
        self.assertIn("trainable_example_count=19", report)
        self.assertIn("fallback_count=1", report)

    def test_run_seed_offsets_seed_by_run_index(self) -> None:
        self.assertIsNone(_run_seed(None, 0))
        self.assertEqual(7, _run_seed(7, 0))
        self.assertEqual(8, _run_seed(7, 1))


def _item(
    run_index: int,
    rank: int,
    slot_type: str,
    movie_id: str,
    title: str,
    watched: bool = False,
    sources: tuple[str, ...] = ("douban_top250:top1",),
) -> RecommendationEvaluationItem:
    return RecommendationEvaluationItem(
        run_index=run_index,
        rank=rank,
        slot_type=slot_type,
        movie_id=movie_id,
        title=title,
        year=2024,
        score=8.5,
        douban_rating=8.0,
        watched=watched,
        pool_sources=sources,
    )


if __name__ == "__main__":
    unittest.main()
