from copy import deepcopy
from pathlib import Path
import json
import unittest

from exhibit_affinity.data import load_bundle, visible_snapshot
from exhibit_affinity.product import *


class ProductTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.snapshot = visible_snapshot(load_bundle(root / "fixtures/bundle.json"), "2026-01-01")

    def test_overview_is_finite_and_preserves_sources(self):
        original = deepcopy(self.snapshot)
        result = summarize(self.snapshot)
        self.assertTrue(result)
        self.assertIn("interpretation", result)
        json.dumps(result, allow_nan=False)
        self.assertEqual(self.snapshot, original)

    def test_revoked_item_removed_from_popularity(self):
        self.assertNotIn("item-004", eligible_items(self.snapshot))
        self.assertNotIn("item-004", [row["item"] for row in popular_candidates(self.snapshot, 100)])

    def test_action_must_match_display(self):
        self.snapshot["tables"]["actions"][0]["user"] = "wrong-user"
        with self.assertRaises(ValueError):
            joined_actions(self.snapshot)
