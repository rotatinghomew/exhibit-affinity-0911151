from copy import deepcopy
from pathlib import Path
import json
import unittest

from exhibit_affinity.data import load_bundle, visible_snapshot
from exhibit_affinity.recommend import (
    build_global_model,
    build_user_profile,
    cosine,
    eligible_catalog,
    item_catalog,
    positive_events,
    recommend,
    score_item,
)

ROOT = Path(__file__).resolve().parents[1]
UNEXPOSED = ["item-064", "item-065", "item-066", "item-067", "item-068", "item-069", "item-070", "item-071"]


class RecommendTests(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle(ROOT / "fixtures/bundle.json")
        self.snapshot = visible_snapshot(self.bundle, "2026-01-01")

    def test_recommendation_is_deterministic_and_ranked(self):
        first = recommend(self.snapshot, "editor-00", limit=12)
        second = recommend(self.snapshot, "editor-00", limit=12)
        self.assertEqual(first, second)
        scores = [row["score"] for row in first]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(len(first), 12)
        self.assertTrue(all(row["item"] in eligible_catalog(self.snapshot) for row in first))

    def test_only_observed_items_are_marked_validated(self):
        rows = recommend(self.snapshot, "editor-00", limit=72)
        global_model = build_global_model(self.snapshot)
        for row in rows:
            expected_exposed = row["item"] in global_model["item_impressions"]
            self.assertEqual(row["exposed"], expected_exposed)
            self.assertEqual(row["offline_validated"], expected_exposed)
        cold = [row for row in rows if row["item"] in UNEXPOSED]
        self.assertEqual(len(cold), len(UNEXPOSED))
        self.assertTrue(all(row["evidence"] == "catalog-only" for row in cold))
        self.assertTrue(all(not row["offline_validated"] for row in cold))
        self.assertTrue(all(row["global_signal"] == 0.0 for row in cold))

    def test_unexposed_items_are_never_used_as_negative_feedback(self):
        # 没有展示机会的作品不能进入任何用户反馈统计。
        events = positive_events(self.snapshot)
        self.assertFalse(any(event["item"] in UNEXPOSED for event in events))
        global_model = build_global_model(self.snapshot)
        for item in UNEXPOSED:
            self.assertNotIn(item, global_model["item_prior"])
            self.assertNotIn(item, global_model["item_impressions"])

    def test_personalization_differs_across_editors(self):
        a = [row["item"] for row in recommend(self.snapshot, "editor-00", limit=10)]
        b = [row["item"] for row in recommend(self.snapshot, "editor-19", limit=10)]
        self.assertNotEqual(a, b)

    def test_new_editor_falls_back_to_global_and_content(self):
        rows = recommend(self.snapshot, "editor-brand-new", limit=10)
        self.assertTrue(all(row["user_support"] == 0.0 for row in rows))
        self.assertTrue(all(row["personal_signal"] == 0.0 for row in rows))
        # 仍然给出有限、确定、可解释的结果。
        json.dumps(rows, allow_nan=False)
        self.assertEqual(rows, recommend(self.snapshot, "editor-brand-new", limit=10))

    def test_brand_new_item_is_catalog_only(self):
        snapshot = deepcopy(self.snapshot)
        snapshot["tables"]["items"].append(
            {"id": "item-900", "revision": 1, "released_at": "2024-01-01",
             "family": "family-99", "museum": "museum-9", "language": "fr",
             "attributes": [0.5, 0.5, 0.5, 0.5, 0.5]}
        )
        snapshot["tables"]["eligibility"].append(
            {"id": "item-900", "revision": 1, "released_at": "2024-01-01",
             "item": "item-900", "allowed": True}
        )
        rationale = score_item(snapshot, "editor-00", "item-900")
        self.assertEqual(rationale["evidence"], "catalog-only")
        self.assertFalse(rationale["offline_validated"])

    def test_family_proxy_uses_sibling_feedback(self):
        # 构造一个同系列作品：系列内有其它作品被展示，新作品自身没有展示。
        snapshot = deepcopy(self.snapshot)
        target_family = "family-00"
        sibling = next(item for item in snapshot["tables"]["items"] if item["family"] == target_family)
        new_id = "item-901"
        snapshot["tables"]["items"].append(
            {**sibling, "id": new_id, "family": target_family,
             "attributes": [0.1, 0.1, 0.1, 0.1, 0.1]}
        )
        snapshot["tables"]["eligibility"].append(
            {"id": new_id, "revision": 1, "released_at": "2024-01-01",
             "item": new_id, "allowed": True}
        )
        rationale = score_item(snapshot, "editor-00", new_id)
        self.assertEqual(rationale["evidence"], "family-proxy")
        self.assertFalse(rationale["offline_validated"])
        self.assertGreater(rationale["family_signal"], 0.0)

    def test_candidate_scope_is_respected(self):
        scope = ["item-021", "item-048"]
        rows = recommend(self.snapshot, "editor-00", limit=10, candidates=scope)
        self.assertEqual({row["item"] for row in rows}, set(scope))

    def test_unknown_candidate_is_rejected(self):
        with self.assertRaises(ValueError):
            recommend(self.snapshot, "editor-00", candidates=["item-xyz"])

    def test_all_scores_finite_and_json_safe(self):
        payload = recommend(self.snapshot, "editor-00", limit=72)
        json.dumps(payload, allow_nan=False)

    def test_cosine_handles_zero_vectors(self):
        self.assertEqual(cosine([0.0, 0.0], [1.0, 2.0]), 0.0)

    def test_profile_aggregates_signal_weights(self):
        profile = build_user_profile(self.snapshot, "editor-00")
        self.assertGreater(profile["support"], 0.0)
        self.assertEqual(len(profile["direction"]), 5)

    def test_recommendation_does_not_mutate_snapshot(self):
        before = deepcopy(self.snapshot)
        recommend(self.snapshot, "editor-00", limit=10)
        self.assertEqual(self.snapshot, before)

    def test_action_mismatch_still_raises(self):
        snapshot = deepcopy(self.snapshot)
        snapshot["tables"]["actions"][0]["user"] = "wrong-user"
        with self.assertRaises(ValueError):
            positive_events(snapshot)

    def test_item_count_matches_catalog(self):
        rows = recommend(self.snapshot, "editor-00", limit=1000)
        self.assertEqual(len(rows), len(eligible_catalog(self.snapshot)))
        self.assertLessEqual(len(rows), len(item_catalog(self.snapshot)))


if __name__ == "__main__":
    unittest.main()
