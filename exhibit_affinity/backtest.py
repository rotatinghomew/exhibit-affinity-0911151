"""按时间回看的离线策略比较（temporal-backtest-v1）。

评估口径与真实选品流程保持一致，刻意不使用随机切分：

1. 训练只使用回看截止日之前公开的资料（visible_snapshot 的同一套可见性规则）；
2. 每个测试单元是真实发生过的一次展示集合（slate），候选范围只包含该集合中
   当时资格仍有效的作品，而不是全目录；标签是该集合之后编辑真实发生的操作；
3. 成套作品（family）不允许跨训练/验证制造好成绩：另设整组留出折，
   被留出系列的全部记录同时从训练侧移除；
4. 没有展示机会的作品与系列没有标签，其离线收益显式标记为未验证，
   不计入任何命中率；
5. propensity 不是随机试验概率，只作为带截断的 IPS 诊断量，
   主结论使用未加权的排序指标。
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import date, timedelta

from .data import visible_snapshot
from .recommend import (
    SIGNAL_WEIGHT,
    build_global_model,
    build_user_profile,
    eligible_catalog,
    item_catalog,
    joined_impressions,
    score_item,
)

STRATEGY = "personalized-candidates-v1"
BASELINES = ("global-popularity", "shuffled-catalog")
DEFAULT_BLOCK = 10
DEFAULT_TOP_K = 3
FAR_FUTURE = "9999-12-31"


def day_before(iso_date):
    """训练可见上界：严格早于测试集合发生日的最近一个日期。"""
    return (date.fromisoformat(iso_date) - timedelta(days=1)).isoformat()
IPS_CLIP_QUANTILE = 0.9
# 同一 slate 内没有任何正向操作时不参与排序指标，只计入覆盖统计。


def _slate_index(slate):
    return int(slate.rsplit("-", 1)[1])


def ordered_slates(snapshot):
    """按展示机会发生时间排列的 slate 序列，返回 (slate, 截止日期)。"""
    dates = defaultdict(list)
    for row in joined_impressions(snapshot):
        dates[row["slate"]].append(row["released_at"])
    ordered = sorted(dates, key=lambda slate: (min(dates[slate]), _slate_index(slate)))
    return [(slate, min(dates[slate])) for slate in ordered]


def cutoff_snapshot(source, as_of):
    """复用资料服务的可见性规则，构造回看截止日的只读快照。

    source 可以是原始 bundle 或已经冻结过的全量快照。注意全量快照只保留
    每条记录的最新代次，历史修订链已经折叠；因此当 as_of 早于快照中某些
    记录的公开日期时，快照源无法还原被修订覆盖前的旧版本，结果会与 bundle
    源不同。需要逐历史日期重建视图的正式回看必须传入原始 bundle；
    快照源仅用于取全量（as_of= FAR_FUTURE）视图。
    """
    if "tables" in source and "schema_version" in source and "snapshot_id" in source:
        source = {
            "schema_version": source["schema_version"],
            "product": source["product"],
            "tables": source["tables"],
        }
    return visible_snapshot(source, as_of)


def slate_candidates(snapshot, slate):
    """一次真实展示集合内、当时资格仍有效的候选作品。"""
    eligible = eligible_catalog(snapshot)
    items = set()
    for row in joined_impressions(snapshot):
        if row["slate"] == slate and row["item"] in eligible:
            items.add(row["item"])
    return items


def slate_labels(snapshot, slate):
    """该展示集合之后真实发生的正向操作，按编辑聚合。"""
    impressions = {row["id"]: row for row in joined_impressions(snapshot)}
    labels = defaultdict(dict)
    for action in snapshot["tables"]["actions"]:
        impression = impressions.get(action["impression"])
        if impression is None or impression["slate"] != slate:
            continue
        labels[impression["user"]][action["item"]] = SIGNAL_WEIGHT.get(action["action"], 1.0)
    return {user: dict(mapping) for user, mapping in labels.items()}


def _dcg(grades):
    return sum(grade / math.log2(position + 2.0) for position, grade in enumerate(grades))


def ranking_metrics(ranked, relevant, k):
    """单个评估单元的排序指标；relevant 为 {item: 权重}。"""
    if not relevant:
        return None
    top = ranked[:k]
    hits = [1 if item in relevant else 0 for item in top]
    precision = sum(hits) / k
    retrieved = sum(hits)
    recall = retrieved / len(relevant)
    gains = 0.0
    for position, item in enumerate(top):
        if item in relevant:
            gains += relevant[item] / math.log2(position + 2.0)
    ideal = _dcg(sorted(relevant.values(), reverse=True)[:k])
    ndcg = gains / ideal if ideal > 0.0 else 0.0
    mrr = 0.0
    for position, hit in enumerate(hits):
        if hit:
            mrr = 1.0 / (position + 1)
            break
    return {
        "precision_at_k": precision,
        "recall_at_k": recall,
        "ndcg_at_k": ndcg,
        "mrr": mrr,
        "support": 1,
    }


def _aggregate(rows):
    if not rows:
        return {
            "precision_at_k": None,
            "recall_at_k": None,
            "ndcg_at_k": None,
            "mrr": None,
            "evaluated_units": 0,
        }
    keys = ("precision_at_k", "recall_at_k", "ndcg_at_k", "mrr")
    summary = {key: sum(row[key] for row in rows) / len(rows) for key in keys}
    return {**{key: round(value, 9) for key, value in summary.items()}, "evaluated_units": len(rows)}


def _ips_diagnostics(units, clip_quantile):
    """带截断的 IPS 诊断。

    propensity 是日志记录的机会概率而非随机试验，因此这里只报告
    截断后的加权命中率作为诊断，不参与策略取舍的主结论。
    """
    propensities = sorted(unit["propensity"] for unit in units)
    if not propensities:
        return {"clip_value": None, "weighted_hit_rate": None, "unweighted_hit_rate": None, "units": 0}
    clip = propensities[min(len(propensities) - 1, int(clip_quantile * (len(propensities) - 1)))]
    weighted = 0.0
    unweighted = 0.0
    for unit in units:
        weight = 1.0 / max(unit["propensity"], clip)
        weighted += weight * unit["hit"]
        unweighted += unit["hit"]
    total_weight = sum(1.0 / max(unit["propensity"], clip) for unit in units)
    return {
        "clip_value": round(clip, 9),
        "weighted_hit_rate": round(weighted / total_weight, 9),
        "unweighted_hit_rate": round(unweighted / len(units), 9),
        "units": len(units),
        "note": "propensity 非随机试验，IPS 仅作诊断，策略取舍以未加权排序指标为准",
    }


def _personalized_ranking(training_model, profile, user, candidates, k):
    rows = [
        score_item(training_model["snapshot"], user, item, profile, training_model["global"])
        for item in candidates
    ]
    rows.sort(key=lambda row: (-row["score"], row["item"]))
    return [row["item"] for row in rows[:k]]


def _popularity_ranking(global_model, candidates, k):
    rows = list(candidates)
    rows.sort(key=lambda item: (-global_model["item_prior"].get(item, 0.0), item))
    return rows[:k]


def _shuffled_ranking(candidates, fold_seed, k):
    rows = list(candidates)
    rows.sort(key=lambda item: hashlib.sha256((fold_seed + "|" + item).encode()).hexdigest())
    return rows[:k]


def _families_of(snapshot, items):
    catalog = item_catalog(snapshot)
    return {catalog[item]["family"] for item in items if item in catalog}


def _without_families(snapshot, removed_families):
    """整组留出：把指定系列的全部展示与操作从训练快照中剔除。"""
    if not removed_families:
        return snapshot
    tables = snapshot["tables"]
    kept_impressions = [
        row for row in tables["impressions"]
        if item_catalog(snapshot)[row["item"]]["family"] not in removed_families
    ]
    kept_ids = {row["id"] for row in kept_impressions}
    kept_actions = [row for row in tables["actions"] if row["impression"] in kept_ids]
    narrowed = {**snapshot, "tables": {**tables, "impressions": kept_impressions, "actions": kept_actions}}
    return narrowed


def _evaluate_fold(source, full_snapshot, train_as_of, test_start_date, test_slates,
                   k, fold_seed, holdout_families, snapshot_cache=None):
    # 训练侧：只包含严格早于测试集合发生日的资料；整组留出再剔除指定系列。
    snapshot_cache = {} if snapshot_cache is None else snapshot_cache
    def historical_snapshot(as_of):
        if as_of not in snapshot_cache:
            snapshot_cache[as_of] = cutoff_snapshot(source, as_of)
        return snapshot_cache[as_of]

    snapshot = historical_snapshot(train_as_of)
    training = _without_families(snapshot, holdout_families)
    # 训练模型在整折内只构建一次；用户画像按编辑缓存。
    training_model = {"snapshot": training, "global": build_global_model(training)}
    profile_cache = {}

    def profile_for(user):
        if user not in profile_cache:
            profile_cache[user] = build_user_profile(training, user)
        return profile_cache[user]

    full_catalog = item_catalog(full_snapshot)
    seen_families = _families_of(
        training, {row["item"] for row in joined_impressions(training)}
    )
    per_strategy = {name: [] for name in (STRATEGY, *BASELINES)}
    ips_units = []
    evaluated = 0
    slates_without_positive_action = 0
    candidate_units = 0
    stratified = {"family_seen": [], "family_unseen": []}
    holdout_hits = {"eligible_units": 0, "evaluated_units": 0, STRATEGY: [], "global-popularity": []}
    slate_dates = {slate: date for slate, date in ordered_slates(full_snapshot)}
    historical_impressions = {}

    for slate in test_slates:
        # 候选范围按该集合发生当日的历史资格冻结；标签是之后才公开的真实结果，
        # 因此标签取自全量资料，而不是当日快照。
        historical = historical_snapshot(slate_dates[slate])
        historical_impressions[slate] = {
            row["user"]: row for row in joined_impressions(historical)
        }
        candidates = slate_candidates(historical, slate)
        labels = slate_labels(full_snapshot, slate)
        if not labels:
            slates_without_positive_action += 1
        # 只评估训练截止前已经在展示记录中出现过的编辑，模拟真实可选编辑。
        known_users = {row["user"] for row in joined_impressions(training)}
        for user, relevant in labels.items():
            if user not in known_users:
                continue
            pool = candidates
            if holdout_families:
                pool = {item for item in pool if full_catalog[item]["family"] in holdout_families}
                if not pool:
                    continue
            personal_ranked = _personalized_ranking(
                training_model, profile_for(user), user, pool, k
            )
            popularity_ranked = _popularity_ranking(training_model["global"], pool, k)
            shuffled_ranked = _shuffled_ranking(pool, fold_seed + ":" + user, k)
            rankings = {
                STRATEGY: personal_ranked,
                "global-popularity": popularity_ranked,
                "shuffled-catalog": shuffled_ranked,
            }
            for name, ranked in rankings.items():
                metric = ranking_metrics(ranked, relevant, k)
                if metric is not None:
                    per_strategy[name].append(metric)
            evaluated += 1
            candidate_units += len(pool)
            relevant_families = _families_of(full_snapshot, relevant)
            bucket = (
                stratified["family_seen"]
                if relevant_families.issubset(seen_families)
                else stratified["family_unseen"]
            )
            bucket.append(ranking_metrics(personal_ranked, relevant, k))
            impression = historical_impressions[slate].get(user)
            if impression is not None:
                ips_units.append(
                    {
                        "propensity": impression["propensity"],
                        "hit": 1 if any(item in relevant for item in personal_ranked) else 0,
                    }
                )
            if holdout_families:
                holdout_hits["eligible_units"] += 1
                holdout_hits["evaluated_units"] += 1
                holdout_hits[STRATEGY].append(ranking_metrics(personal_ranked, relevant, k))
                holdout_hits["global-popularity"].append(ranking_metrics(popularity_ranked, relevant, k))

    fold = {
        "train_as_of": train_as_of,
        "test_start_date": test_start_date,
        "test_slates": test_slates,
        "holdout_families": sorted(holdout_families),
        "metrics": {name: _aggregate(rows) for name, rows in per_strategy.items()},
        "stratified": {
            "family_seen": _aggregate(stratified["family_seen"]),
            "family_unseen": _aggregate(stratified["family_unseen"]),
        },
        "ips_diagnostics": _ips_diagnostics(ips_units, IPS_CLIP_QUANTILE),
        "evaluated_units": evaluated,
        "slates_without_positive_action": slates_without_positive_action,
        "mean_candidate_pool": round(candidate_units / evaluated, 9) if evaluated else None,
    }
    if holdout_families:
        fold["group_holdout"] = {
            "families": sorted(holdout_families),
            "eligible_units": holdout_hits["eligible_units"],
            "metrics": {
                STRATEGY: _aggregate(holdout_hits[STRATEGY]),
                "global-popularity": _aggregate(holdout_hits["global-popularity"]),
            },
            "interpretation": "整组留出折：被留出系列的全部记录同时退出训练，验证不依赖同系列另一半的数据泄漏",
        }
    return fold


def run_walk_forward(source, block=DEFAULT_BLOCK, top_k=DEFAULT_TOP_K):
    """滚动时间窗回看：训练窗始终早于测试 slate，候选范围等于真实展示集合。

    source 通常是含完整修订代次的原始 bundle；传入冻结快照时只能得到
    快照自身口径下的结果，无法还原被折叠的历史修订（见 cutoff_snapshot）。
    """
    if block < 1 or top_k < 1:
        raise ValueError("回看块大小与 top_k 应为正数")
    full_snapshot = cutoff_snapshot(source, FAR_FUTURE)
    timeline = ordered_slates(full_snapshot)
    slate_names = [slate for slate, _ in timeline]
    fold_specs = []
    for start in range(block, len(timeline), block):
        end = min(start + block, len(timeline))
        test_start_date = timeline[start][1]
        train_as_of = day_before(test_start_date)
        test_slates = slate_names[start:end]
        fold_specs.append((train_as_of, test_start_date, test_slates))

    folds = []
    snapshot_cache = {FAR_FUTURE: full_snapshot}
    for index, (train_as_of, test_start_date, test_slates) in enumerate(fold_specs):
        folds.append(
            _evaluate_fold(
                source,
                full_snapshot,
                train_as_of,
                test_start_date,
                test_slates,
                top_k,
                "fold-%d" % index,
                holdout_families=set(),
                snapshot_cache=snapshot_cache,
            )
        )

    # 整组留出折：取首个测试块，把首次曝光就落在该块内的系列整组移出训练。
    first_train_as_of, first_test_start, first_test = fold_specs[0]
    early_snapshot = snapshot_cache.setdefault(
        first_train_as_of, cutoff_snapshot(source, first_train_as_of)
    )
    exposed_before = _families_of(
        early_snapshot, {row["item"] for row in joined_impressions(early_snapshot)}
    )
    test_snapshot = full_snapshot
    debut_in_block = set()
    for slate in first_test:
        for row in joined_impressions(test_snapshot):
            if row["slate"] == slate:
                family = item_catalog(test_snapshot)[row["item"]]["family"]
                if family not in exposed_before:
                    debut_in_block.add(family)
    grouped_fold = _evaluate_fold(
        source,
        full_snapshot,
        first_train_as_of,
        first_test_start,
        first_test,
        top_k,
        "group-holdout",
        holdout_families=debut_in_block,
        snapshot_cache=snapshot_cache,
    )

    exposed_families = _families_of(
        test_snapshot, {row["item"] for row in joined_impressions(test_snapshot)}
    )
    all_families = {row["family"] for row in test_snapshot["tables"]["items"]}
    never_exposed = sorted(all_families - exposed_families)
    never_exposed_items = sorted(
        row["id"] for row in test_snapshot["tables"]["items"] if row["family"] in never_exposed
    )
    return {
        "method": "temporal-backtest-v1",
        "block_size": block,
        "top_k": top_k,
        "split_rule": "按 slate 发生时间滚动切分，训练资料公开日期严格早于测试集合；不使用随机切分",
        "candidate_rule": "每个评估单元的候选只包含该真实展示集合内当时资格有效的作品",
        "folds": folds,
        "group_holdout_fold": grouped_fold,
        "unexposed_segment": {
            "families": never_exposed,
            "items": never_exposed_items,
            "offline_validated": False,
            "interpretation": "该段内容在全部历史中没有展示机会，没有标签可计算命中率，离线收益未验证",
        },
        "baselines": list(BASELINES),
        "primary_evidence": "未加权 precision_at_k / recall_at_k / ndcg_at_k / mrr，按时间折与系列分层汇总",
    }


def delta_vs_popularity(report):
    """汇总个性化策略相对全站热门基线的逐折差值，作为接入实际选品的证据。"""
    result = []
    for fold in report["folds"]:
        personal = fold["metrics"][STRATEGY]
        popular = fold["metrics"]["global-popularity"]
        if personal["evaluated_units"] == 0:
            continue
        result.append(
            {
                "train_as_of": fold["train_as_of"],
                "test_slates": fold["test_slates"],
                "evaluated_units": personal["evaluated_units"],
                "precision_delta": round(
                    personal["precision_at_k"] - popular["precision_at_k"], 9
                ),
                "ndcg_delta": round(personal["ndcg_at_k"] - popular["ndcg_at_k"], 9),
                "mrr_delta": round(personal["mrr"] - popular["mrr"], 9),
            }
        )
    return result
