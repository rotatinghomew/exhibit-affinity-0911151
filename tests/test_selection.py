from pathlib import Path
import json
import tempfile
import unittest

from exhibit_affinity.client import ProductClient
from exhibit_affinity.data import load_bundle, visible_snapshot
from exhibit_affinity.selection import (
    BACKTEST_METHOD,
    SELECTION_METHOD,
    apply_latest_policy,
    build_evidence,
    build_selection,
    build_selection_artifact,
)
from exhibit_affinity.service import running_service
from exhibit_affinity.store import ArtifactStore
from exhibit_affinity.workflow import publish_personalized_selection, publish_temporal_backtest

ROOT = Path(__file__).resolve().parents[1]


class SelectionUnitTests(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle(ROOT / "fixtures/bundle.json")
        self.snapshot = visible_snapshot(self.bundle, "2026-01-01")

    def test_selection_contains_rationale_and_evidence_flags(self):
        selection = build_selection(self.snapshot, users=["editor-00"], limit=10)
        self.assertEqual(selection["method"], SELECTION_METHOD)
        entries = selection["selections"][0]["entries"]
        self.assertEqual(len(entries), 10)
        for entry in entries:
            self.assertIn("signals", entry)
            self.assertIn(entry["evidence"], ("observed", "family-proxy", "catalog-only"))
        json.dumps(selection, allow_nan=False)

    def test_policy_revocation_removes_item_but_keeps_rationale(self):
        selection = build_selection(self.snapshot, users=["editor-00"], limit=12)
        target = selection["selections"][0]["entries"][0]["item"]
        policy = {"revision": 3, "blocked_items": [target]}
        items, withdrawn = apply_latest_policy(selection, policy)
        self.assertNotIn(target, items)
        self.assertEqual(len(withdrawn), 1)
        record = withdrawn[0]
        self.assertEqual(record["item"], target)
        self.assertEqual(record["policy_revision"], 3)
        # 原始推荐依据完整保留。
        self.assertEqual(record["original_rationale"]["item"], target)
        self.assertIsNotNone(record["original_rationale"]["score"])
        # 原对象不被修改。
        self.assertIn(target, [e["item"] for e in selection["selections"][0]["entries"]])

    def test_other_editors_keep_their_entries_after_revocation(self):
        selection = build_selection(self.snapshot, users=["editor-00", "editor-01"], limit=12)
        target = selection["selections"][0]["entries"][0]["item"]
        items, withdrawn = apply_latest_policy(
            selection, {"revision": 2, "blocked_items": [target]}
        )
        remaining_editors = {e["user"] for e in selection["selections"]}
        self.assertEqual(remaining_editors, {"editor-00", "editor-01"})
        self.assertTrue(all(item != target for item in items))

    def test_artifact_carries_backtest_evidence(self):
        evidence = build_evidence(self.snapshot)
        artifact = build_selection_artifact(
            self.snapshot, {"revision": 1, "blocked_items": []},
            users=["editor-00"], limit=8, evidence=evidence,
        )
        self.assertEqual(artifact["method"], SELECTION_METHOD)
        self.assertEqual(artifact["evidence_artifact_method"], BACKTEST_METHOD)
        self.assertEqual(artifact["offline_evidence"]["method"], BACKTEST_METHOD)
        self.assertFalse(artifact["offline_evidence"]["unexposed_segment"]["offline_validated"])

    def test_evidence_can_be_omitted(self):
        artifact = build_selection_artifact(
            self.snapshot, {"revision": 1, "blocked_items": []},
            users=["editor-00"], limit=8, evidence=None,
        )
        self.assertNotIn("offline_evidence", artifact)

    def test_revoked_cold_item_keeps_unvalidated_rationale(self):
        # 一件没有曝光机会的冷启动作品被 policy 收回时，
        # 仍保留其 catalog-only 依据，且不被误标为已验证。
        from exhibit_affinity.recommend import recommend

        cold = next(
            row["item"]
            for row in recommend(self.snapshot, "editor-00", limit=72)
            if row["evidence"] == "catalog-only"
        )
        selection = build_selection(
            self.snapshot, users=["editor-00"], limit=72, candidates=[cold]
        )
        items, withdrawn = apply_latest_policy(
            selection, {"revision": 4, "blocked_items": [cold]}
        )
        self.assertEqual(items, [])
        self.assertEqual(len(withdrawn), 1)
        rationale = withdrawn[0]["original_rationale"]
        self.assertEqual(rationale["item"], cold)
        self.assertEqual(rationale["evidence"], "catalog-only")
        self.assertFalse(rationale["offline_validated"])


class SelectionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.bundle = load_bundle(ROOT / "fixtures/bundle.json")
        cls.store = ArtifactStore(
            str(Path(cls.directory.name) / "state.sqlite3"), cls.bundle.get("policy")
        )
        cls.running = running_service(cls.bundle, cls.store)
        cls.client = ProductClient(cls.running.__enter__())

    @classmethod
    def tearDownClass(cls):
        cls.running.__exit__(None, None, None)
        cls.store.close()
        cls.directory.cleanup()

    def test_selection_roundtrip_through_http(self):
        receipt = publish_personalized_selection(
            self.client, "2026-01-01", "selection-roundtrip",
            users=["editor-00", "editor-02"], limit=6,
        )
        artifact = receipt["artifact"]
        self.assertEqual(artifact["method"], SELECTION_METHOD)
        self.assertEqual(self.client.artifact(receipt["artifact_id"]), artifact)
        self.assertEqual(artifact["offline_evidence"]["method"], BACKTEST_METHOD)

    def test_revocation_between_taking_order_and_submit(self):
        snapshot = self.client.snapshot("2026-01-01")
        selection = build_selection(snapshot, users=["editor-00"], limit=6)
        target = selection["selections"][0]["entries"][0]["item"]
        policy = self.client.policy()
        # 授权服务在取单之后收回资格。
        self.client.request(
            "/policy",
            {"expected_revision": policy["revision"],
             "policy": {"blocked_items": ["item-004", target]}},
        )
        receipt = publish_personalized_selection(
            self.client, "2026-01-01", "selection-revoked",
            users=["editor-00"], limit=6,
        )
        artifact = receipt["artifact"]
        self.assertNotIn(target, artifact["items"])
        withdrawn = artifact["result"]["withdrawn_by_policy"]
        self.assertEqual([row["item"] for row in withdrawn], [target])
        self.assertIsNotNone(withdrawn[0]["original_rationale"]["score"])
        self.assertEqual(
            artifact["result"]["policy_revision_at_submit"],
            self.client.policy()["revision"],
        )

    def test_store_rejects_blocked_item_as_backstop(self):
        policy = self.client.policy()
        target = "item-021"
        self.client.request(
            "/policy",
            {"expected_revision": policy["revision"],
             "policy": {"blocked_items": ["item-004", target]}},
        )
        receipt = publish_personalized_selection(
            self.client, "2026-01-01", "selection-backstop",
            users=["editor-00"], limit=6,
        )
        self.assertNotIn(target, receipt["artifact"]["items"])

    def test_backtest_artifact_published_separately(self):
        receipt = publish_temporal_backtest(self.client, "2026-01-01", "backtest-only")
        self.assertEqual(receipt["artifact"]["method"], BACKTEST_METHOD)
        self.assertEqual(
            self.client.artifact(receipt["artifact_id"])["method"], BACKTEST_METHOD
        )

    def test_replay_same_request_key_is_idempotent(self):
        first = publish_personalized_selection(
            self.client, "2026-01-01", "selection-replay",
            users=["editor-01"], limit=5, include_evidence=False,
        )
        second = publish_personalized_selection(
            self.client, "2026-01-01", "selection-replay",
            users=["editor-01"], limit=5, include_evidence=False,
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
