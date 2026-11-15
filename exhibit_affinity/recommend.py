"""个性化候选推荐（personalized-candidates-v1）。

只使用资料快照中真实发生过的展示机会与操作记录建模：没有被展示过的作品
不会产生任何用户反馈，因此既不进入用户偏好，也不被当作负样本，只能走基于
目录内容的冷启动路径，并在结果中显式标注其离线依据状态。

所有打分都是确定性的：同一份快照、同一组参数必然得到同一份名单。
"""
from __future__ import annotations

import math
from collections import Counter

# open 与 save 是不同强度的正向信号；未操作的展示是弱负反馈，不单独计数。
SIGNAL_WEIGHT = {"open": 1.0, "save": 2.0}
# 个性化、同系列信号、全局先验、内容相似的混合权重。
SCORE_WEIGHTS = {"personal": 0.55, "family": 0.20, "global": 0.15, "content": 0.10}
MIN_POSITIVE_SUPPORT = 1e-12


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("打分只接受有限数值")
    if not math.isfinite(value):
        raise ValueError("打分出现非有限值")
    return float(value)


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def _norm(vector):
    return math.sqrt(sum(value * value for value in vector))


def cosine(left, right):
    """目录属性向量上的余弦相似，零向量返回 0 而不是 NaN。"""
    denominator = _norm(left) * _norm(right)
    if denominator <= 0.0:
        return 0.0
    return _dot(left, right) / denominator


def item_catalog(snapshot):
    """快照内当前可见的作品目录。"""
    return {row["id"]: row for row in snapshot["tables"]["items"]}


def eligible_catalog(snapshot):
    """历史资格表中当前仍允许的作品；当前提交资格另由消费端 policy 决定。"""
    items = item_catalog(snapshot)
    allowed = {}
    for row in snapshot["tables"]["eligibility"]:
        if row["allowed"] and row["item"] in items:
            allowed[row["item"]] = items[row["item"]]
    return allowed


def joined_impressions(snapshot):
    """展示机会关联作品目录，校验引用完整性。"""
    items = item_catalog(snapshot)
    joined = []
    for row in snapshot["tables"]["impressions"]:
        if row["item"] not in items:
            raise ValueError("展示机会引用了快照外作品")
        joined.append({**row, "family": items[row["item"]]["family"]})
    return joined


def positive_events(snapshot):
    """真实操作（正向反馈）事件；未操作的展示不构造成负样本。"""
    impressions = {row["id"]: row for row in joined_impressions(snapshot)}
    events = []
    for action in snapshot["tables"]["actions"]:
        impression = impressions.get(action["impression"])
        if impression is None:
            continue
        if action["item"] != impression["item"] or action["user"] != impression["user"]:
            raise ValueError("操作与原展示机会不一致")
        weight = SIGNAL_WEIGHT.get(action["action"])
        if weight is None:
            raise ValueError("未知操作类型: " + str(action["action"]))
        events.append(
            {
                "user": action["user"],
                "item": action["item"],
                "family": impression["family"],
                "action": action["action"],
                "weight": weight,
                "propensity": impression["propensity"],
                "released_at": action["released_at"],
            }
        )
    return events


def build_global_model(snapshot):
    """全局先验：作品与系列层面的正向反馈强度，带置信收缩。

    没有任何展示机会的作品不会出现在统计里，其先验保持为 0，
    由冷启动路径单独处理。
    """
    events = positive_events(snapshot)
    impressions = joined_impressions(snapshot)
    item_weight = Counter()
    item_impressions = Counter()
    family_weight = Counter()
    family_impressions = Counter()
    for impression in impressions:
        item_impressions[impression["item"]] += 1
        family_impressions[impression["family"]] += 1
    for event in events:
        item_weight[event["item"]] += event["weight"]
        family_weight[event["family"]] += event["weight"]
    # 用全站平均反馈率做收缩，避免少量展示即得极端先验。
    total_weight = sum(item_weight.values())
    total_impressions = sum(item_impressions.values())
    base_rate = total_weight / total_impressions if total_impressions else 0.0
    item_prior = {}
    for item, count in item_impressions.items():
        item_prior[item] = (item_weight[item] + base_rate * 2.0) / (count + 2.0)
    family_prior = {}
    for family, count in family_impressions.items():
        family_prior[family] = (family_weight[family] + base_rate * 2.0) / (count + 2.0)
    return {
        "item_prior": item_prior,
        "family_prior": family_prior,
        "item_impressions": dict(item_impressions),
        "family_impressions": dict(family_impressions),
        "base_rate": base_rate,
    }


def build_user_profile(snapshot, user, events=None):
    """单个编辑的偏好画像：属性方向、系列偏好、馆别/语言偏好。"""
    catalog = item_catalog(snapshot)
    events = positive_events(snapshot) if events is None else events
    mine = [event for event in events if event["user"] == user]
    dimension = len(next(iter(catalog.values()))["attributes"]) if catalog else 0
    direction = [0.0] * dimension
    support = 0.0
    family_weight = Counter()
    museum_weight = Counter()
    language_weight = Counter()
    for event in mine:
        item = catalog[event["item"]]
        weight = event["weight"]
        direction = [acc + weight * value for acc, value in zip(direction, item["attributes"])]
        support += weight
        family_weight[item["family"]] += weight
        museum_weight[item["museum"]] += weight
        language_weight[item["language"]] += weight
    if support > 0.0:
        direction = [value / support for value in direction]
    return {
        "user": user,
        "support": support,
        "direction": direction,
        "family_weight": dict(family_weight),
        "museum_weight": dict(museum_weight),
        "language_weight": dict(language_weight),
    }


def _family_affinity(profile, family):
    total = profile["support"]
    if total <= 0.0:
        return 0.0
    return profile["family_weight"].get(family, 0.0) / total


def _categorical_affinity(profile, item):
    total = profile["support"]
    if total <= 0.0:
        return 0.0
    museum = profile["museum_weight"].get(item["museum"], 0.0) / total
    language = profile["language_weight"].get(item["language"], 0.0) / total
    return 0.5 * museum + 0.5 * language


def score_item(snapshot, user, item, profile=None, global_model=None):
    """对单个作品打分并返回可解释依据。

    依据分四类：
    - personal ：该编辑自己的历史正向反馈（内容方向 + 类别 + 系列）；
    - family   ：同系列作品得到的反馈（成套作品之间的关联信号）；
    - global   ：全站收缩先验，需要该作品或同系列被展示过；
    - content  ：纯目录内容相似，不依赖任何反馈，冷启动也可计算。
    """
    catalog = item_catalog(snapshot)
    if item not in catalog:
        raise ValueError("候选作品不在快照目录内")
    profile = build_user_profile(snapshot, user) if profile is None else profile
    global_model = build_global_model(snapshot) if global_model is None else global_model
    entry = catalog[item]
    family = entry["family"]

    if profile["support"] > 0.0:
        content_match = cosine(profile["direction"], entry["attributes"])
        personal = 0.6 * content_match + 0.4 * _categorical_affinity(profile, entry)
    else:
        content_match = 0.0
        personal = 0.0
    family_affinity = _family_affinity(profile, family)
    family_prior = global_model["family_prior"].get(family, 0.0)
    item_prior = global_model["item_prior"].get(item, 0.0)
    family_signal = max(family_affinity, family_prior)
    global_signal = item_prior

    # 纯内容路径：与该编辑历史正向作品的属性相似；新编辑无历史时为 0。
    content_only = content_match
    score = (
        SCORE_WEIGHTS["personal"] * personal
        + SCORE_WEIGHTS["family"] * family_signal
        + SCORE_WEIGHTS["global"] * global_signal
        + SCORE_WEIGHTS["content"] * content_only
    )
    score = _finite(score)
    exposed = item in global_model["item_impressions"]
    family_exposed = family in global_model["family_impressions"]
    if exposed:
        evidence = "observed"
    elif family_exposed:
        # 作品本身没有反馈，但同系列其它作品被展示并反馈过。
        evidence = "family-proxy"
    else:
        # 整个系列都没有展示机会：只有目录内容可以作为依据。
        evidence = "catalog-only"
    rationale = {
        "item": item,
        "family": family,
        "score": round(score, 9),
        "evidence": evidence,
        "exposed": exposed,
        "personal_signal": round(personal, 9),
        "family_signal": round(family_signal, 9),
        "global_signal": round(global_signal, 9),
        "content_signal": round(content_only, 9),
        "user_support": profile["support"],
        "offline_validated": exposed,
    }
    return rationale


def recommend(snapshot, user, limit=10, candidates=None):
    """个性化候选名单。

    candidates 为 None 时默认取快照内历史资格仍允许的全部作品；
    传入候选集合可让回放与真实选品范围保持一致。
    """
    if limit < 1:
        raise ValueError("候选数量应为正数")
    catalog = item_catalog(snapshot)
    if candidates is None:
        candidates = set(eligible_catalog(snapshot))
    candidates = list(dict.fromkeys(candidates))
    unknown = [item for item in candidates if item not in catalog]
    if unknown:
        raise ValueError("候选含快照外作品: " + ",".join(sorted(unknown)))
    profile = build_user_profile(snapshot, user)
    global_model = build_global_model(snapshot)
    rows = [
        score_item(snapshot, user, item, profile, global_model)
        for item in candidates
    ]
    rows.sort(key=lambda row: (-row["score"], row["item"]))
    return rows[:limit]


def recommend_all_users(snapshot, limit=10, candidates=None):
    """为快照内所有在展示记录中出现过的编辑生成名单。"""
    users = sorted({row["user"] for row in joined_impressions(snapshot)})
    return {user: recommend(snapshot, user, limit, candidates) for user in users}
