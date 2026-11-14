from collections import Counter


def eligible_items(snapshot):
    items = {row["id"]: row for row in snapshot["tables"]["items"]}
    return {row["item"]: items[row["item"]] for row in snapshot["tables"]["eligibility"] if row["allowed"] and row["item"] in items}


def joined_actions(snapshot):
    impressions = {row["id"]: row for row in snapshot["tables"]["impressions"]}
    result = []
    for action in snapshot["tables"]["actions"]:
        impression = impressions.get(action["impression"])
        if impression is None:
            continue
        if action["item"] != impression["item"] or action["user"] != impression["user"]:
            raise ValueError("操作与原展示机会不一致")
        result.append({**action, "slate": impression["slate"], "propensity": impression["propensity"]})
    return result


def popular_candidates(snapshot, limit=10):
    if limit < 1:
        raise ValueError("候选数量应为正数")
    candidates = eligible_items(snapshot)
    counts = Counter(row["item"] for row in joined_actions(snapshot))
    return [{"item": identifier, "observed_actions": counts[identifier], "family": candidates[identifier]["family"]} for identifier in sorted(candidates, key=lambda key: (-counts[key], key))[:limit]]


def summarize(snapshot):
    return {"eligible_count": len(eligible_items(snapshot)), "observed_action_count": len(joined_actions(snapshot)), "popular": popular_candidates(snapshot), "interpretation": "全站已观测热门度，不是个性化偏好或收益估计"}
