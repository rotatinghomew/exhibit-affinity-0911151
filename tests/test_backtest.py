from pathlib import Path
import json
import unittest

from exhibit_affinity.backtest import (
    BASELINES,
    STRATEGY,
    _without_families,
    cutoff_snapshot,
    delta_vs_popularity,
    ordered_slates,
    run_walk_forward,
    slate_candidates,
    slate_labels,
)
from exhibit_affinity.data import load_bundle
from exhibit_affinity.recommend import item_catalog, joined_impressions

ROOT = Path(__file__).resolve().parents[1]
UNEXPOSED_FAMILIES = ["family-32", "family-33", "family-34", "family-35"]
UNEXPOSED_ITEMS = ["item-064", "item-065", "item-066", "item-067",
                   "item-068", "item-069", "item-070", "item-071"]


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle(ROOT / "fixtures/bundle.json")
        self.report = run_walk_forward(self.bundle)

    def test_report_is_finite_and_serializable(self):
        json.dumps(self.report, allow_nan=False)
        self.assertEqual(self.report["method"], "temporal-backtest-v1")

    def test_temporal_ordering_no_random_split(self):
        timeline = ordered_slates(cutoff_snapshot(self.bundle, "9999-12-31"))
        dates = [date for _, date in timeline]
        self.assertEqual(dates, sorted(dates))
        for fold in self.report["folds"]:
            # 训练可见上界严格早于该折所有测试集合的发生日。
            self.assertLess(fold["train_as_of"], fold["test_start_date"])
            for slate in fold["test_slates"]:
                self.assertLess(fold["train_as_of"], dict(timeline)[slate])
        self.assertIn("不使用随机切分", self.report["split_rule"])

    def test_training_data_cannot_see_future_records(self):
        fold = self.report["folds"][0]
        training = cutoff_snapshot(self.bundle, fold["train_as_of"])
        for table_rows in training["tables"].values():
            self.assertTrue(all(row["released_at"] <= fold["train_as_of"] for row in table_rows))
        # 测试起始日当天的记录不得出现在训练侧。
        self.assertTrue(
            all(row["released_at"] < fold["test_start_date"] for row in training["tables"]["impressions"])
        )

    def test_candidates_match_real_slate_scope(self):
        full = cutoff_snapshot(self.bundle, "9999-12-31")
        # 每个评估单元的候选必须是该真实展示集合内、当时资格有效的作品，
        # 而不是全目录。
        for fold in self.report["folds"]:
            historical = cutoff_snapshot(self.bundle, fold["train_as_of"])
            for slate in fold["test_slates"]:
                pool = slate_candidates(
                    cutoff_snapshot(self.bundle, dict(ordered_slates(full))[slate]), slate
                )
                slate_only = {
                    row["item"] for row in joined_impressions(full) if row["slate"] == slate
                }
                self.assertTrue(pool.issubset(slate_only))
                self.assertLess(len(pool), len(item_catalog(full)))
        self.assertIn("真实展示集合", self.report["candidate_rule"])

    def test_labels_come_from_real_actions_only(self):
        full = cutoff_snapshot(self.bundle, "9999-12-31")
        for fold in self.report["folds"]:
            for slate in fold["test_slates"]:
                labeled_items = set()
                for mapping in slate_labels(full, slate).values():
                    labeled_items.update(mapping)
                slate_items = {
                    row["item"] for row in joined_impressions(full) if row["slate"] == slate
                }
                self.assertTrue(labeled_items.issubset(slate_items))

    def test_all_strategies_evaluated_on_same_units(self):
        for fold in self.report["folds"]:
            counts = {
                name: fold["metrics"][name]["evaluated_units"]
                for name in (STRATEGY, *BASELINES)
            }
            self.assertEqual(len(set(counts.values())), 1)
            self.assertEqual(fold["evaluated_units"], next(iter(counts.values())))

    def test_group_holdout_keeps_families_on_one_side(self):
        grouped = self.report["group_holdout_fold"]
        families = set(grouped["group_holdout"]["families"])
        self.assertTrue(families)
        base_training = cutoff_snapshot(self.bundle, grouped["train_as_of"])
        training = _without_families(base_training, families)
        trained_families = {
            item_catalog(training)[row["item"]]["family"]
            for row in joined_impressions(training)
        }
        self.assertFalse(families.intersection(trained_families))
        # 被留出系列的首曝日期落在测试块内（训练窗内本就没有其记录），
        # 这正是要验证的冷系列场景：训练侧任何时点都见不到该系列。
        full = cutoff_snapshot(self.bundle, "9999-12-31")
        slate_dates = dict(ordered_slates(full))
        test_start = min(slate_dates[s] for s in grouped["test_slates"])
        for family in families:
            debut = min(
                row["released_at"]
                for row in joined_impressions(full)
                if item_catalog(full)[row["item"]]["family"] == family
            )
            self.assertGreaterEqual(debut, test_start)
        # 即使训练窗没有该系列记录，过滤操作也必须保持幂等且不影响其它系列。
        self.assertEqual(
            len(training["tables"]["impressions"]),
            len(base_training["tables"]["impressions"]),
        )
        self.assertEqual(training["tables"]["items"], base_training["tables"]["items"])

    def test_family_removal_actually_drops_records_when_present(self):
        # 用一个训练窗内确实存在的系列验证整组剔除会真实删除展示与操作。
        fold = self.report["folds"][2]
        base = cutoff_snapshot(self.bundle, fold["train_as_of"])
        present_family = item_catalog(base)[
            joined_impressions(base)[0]["item"]
        ]["family"]
        narrowed = _without_families(base, {present_family})
        removed_impressions = [
            row for row in base["tables"]["impressions"]
            if item_catalog(base)[row["item"]]["family"] == present_family
        ]
        self.assertTrue(removed_impressions)
        self.assertEqual(
            len(narrowed["tables"]["impressions"]),
            len(base["tables"]["impressions"]) - len(removed_impressions),
        )
        removed_ids = {row["id"] for row in removed_impressions}
        self.assertFalse(
            any(row["impression"] in removed_ids for row in narrowed["tables"]["actions"])
        )
        surviving_families = {
            item_catalog(narrowed)[row["item"]]["family"]
            for row in joined_impressions(narrowed)
        }
        self.assertNotIn(present_family, surviving_families)

    def test_group_holdout_metrics_are_honest(self):
        grouped = self.report["group_holdout_fold"]["group_holdout"]
        # 支持量很小时必须如实报告，不能伪造高命中率。
        self.assertGreaterEqual(grouped["eligible_units"], 0)
        for name, metrics in grouped["metrics"].items():
            self.assertEqual(metrics["evaluated_units"], grouped["eligible_units"])

    def test_unexposed_segment_is_explicitly_unvalidated(self):
        segment = self.report["unexposed_segment"]
        self.assertEqual(segment["families"], UNEXPOSED_FAMILIES)
        self.assertEqual(segment["items"], UNEXPOSED_ITEMS)
        self.assertFalse(segment["offline_validated"])
        # 无曝光系列不出现在任何折的评估标签里。
        full = cutoff_snapshot(self.bundle, "9999-12-31")
        exposed_families = {
            item_catalog(full)[row["item"]]["family"] for row in joined_impressions(full)
        }
        self.assertFalse(set(UNEXPOSED_FAMILIES).intersection(exposed_families))

    def test_unexposed_items_never_enter_backtest_candidates(self):
        # 没有展示机会的作品不可能出现在任何真实 slate 候选池里，
        # 因此离线指标根本不计算它们，避免把离线收益解释成已验证。
        full = cutoff_snapshot(self.bundle, "9999-12-31")
        slate_dates = dict(ordered_slates(full))
        for fold in self.report["folds"]:
            for slate in fold["test_slates"]:
                historical = cutoff_snapshot(self.bundle, slate_dates[slate])
                pool = slate_candidates(historical, slate)
                self.assertFalse(pool.intersection(UNEXPOSED_ITEMS))

    def test_ips_is_only_diagnostic(self):
        for fold in self.report["folds"]:
            diagnostics = fold["ips_diagnostics"]
            if diagnostics["units"]:
                self.assertIsNotNone(diagnostics["clip_value"])
                self.assertIn("非随机试验", diagnostics["note"])
                # 截断后的权重命中率与未加权命中率都给出，但不伪装成主指标。
                self.assertIsNotNone(diagnostics["weighted_hit_rate"])

    def test_strategy_comparison_reports_deltas(self):
        deltas = delta_vs_popularity(self.report)
        self.assertTrue(deltas)
        for row in deltas:
            for key in ("precision_delta", "ndcg_delta", "mrr_delta"):
                self.assertIsNotNone(row[key])
                self.assertGreaterEqual(row["evaluated_units"], 1)

    def test_metrics_stay_in_valid_range(self):
        for fold in self.report["folds"]:
            for name, metrics in fold["metrics"].items():
                if metrics["evaluated_units"] == 0:
                    continue
                for key in ("precision_at_k", "recall_at_k", "ndcg_at_k", "mrr"):
                    self.assertGreaterEqual(metrics[key], 0.0)
                    self.assertLessEqual(metrics[key], 1.0)

    def test_bundle_is_authoritative_source_for_historical_cutoffs(self):
        from exhibit_affinity.backtest import FAR_FUTURE

        # 全量视图下两种资料源一致。
        full_snapshot = cutoff_snapshot(self.bundle, FAR_FUTURE)
        self.assertEqual(
            cutoff_snapshot(full_snapshot, FAR_FUTURE)["snapshot_id"],
            full_snapshot["snapshot_id"],
        )
        # 历史截止日必须由原始 bundle 重建：冻结快照折叠了旧修订代次
        # （item-004 的 rev1 资格被 rev2 撤回覆盖），无法还原历史资格。
        historical_from_bundle = cutoff_snapshot(self.bundle, "2024-01-20")
        historical_from_snapshot = cutoff_snapshot(full_snapshot, "2024-01-20")
        bundle_elig = {row["item"] for row in historical_from_bundle["tables"]["eligibility"]}
        snapshot_elig = {row["item"] for row in historical_from_snapshot["tables"]["eligibility"]}
        self.assertIn("item-004", bundle_elig)
        self.assertNotIn("item-004", snapshot_elig)
        # 正式回看以 bundle 为源，结果可复现。
        again = run_walk_forward(self.bundle)
        self.assertEqual(
            [f["metrics"] for f in again["folds"]],
            [f["metrics"] for f in self.report["folds"]],
        )

    def test_invalid_parameters_are_rejected(self):
        with self.assertRaises(ValueError):
            run_walk_forward(self.bundle, block=0)
        with self.assertRaises(ValueError):
            run_walk_forward(self.bundle, top_k=0)


if __name__ == "__main__":
    unittest.main()
