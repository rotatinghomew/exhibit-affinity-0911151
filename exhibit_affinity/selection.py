"""把个性化候选接到实际选品名单（personalized-selection-v1）。

流程对应编辑的真实操作顺序：先取单（生成候选与推荐依据），授权服务随后可能
收回某件作品的展出资格；最终提交必须服从取单之后重新读取的最新 policy，
被收回的作品从最终名单移除，但原始推荐依据完整保留在成果里。
"""
from __future__ import annotations

from .backtest import run_walk_forward
from .client import RemoteError
from .recommend import recommend, recommend_all_users

SELECTION_METHOD = "personalized-selection-v1"
BACKTEST_METHOD = "temporal-backtest-v1"
MAX_POLICY_RETRIES = 3


def build_selection(snapshot, users=None, limit=10, candidates=None):
    """基于快照生成候选名单与逐条推荐依据，不读取消费端 policy。"""
    if users is None:
        rows = recommend_all_users(snapshot, limit, candidates)
    else:
        rows = {user: recommend(snapshot, user, limit, candidates) for user in users}
    selections = []
    for user in sorted(rows):
        entries = []
        for rationale in rows[user]:
            entries.append(
                {
                    "item": rationale["item"],
                    "score": rationale["score"],
                    "evidence": rationale["evidence"],
                    "exposed": rationale["exposed"],
                    "offline_validated": rationale["offline_validated"],
                    "signals": {
                        "personal": rationale["personal_signal"],
                        "family": rationale["family_signal"],
                        "global": rationale["global_signal"],
                        "content": rationale["content_signal"],
                    },
                    "user_support": rationale["user_support"],
                }
            )
        selections.append({"user": user, "entries": entries})
    return {
        "method": SELECTION_METHOD,
        "limit_per_user": limit,
        "selections": selections,
        "interpretation": "个性化候选推荐；catalog-only 条目没有展示机会，离线收益未验证",
    }


def apply_latest_policy(selection, policy):
    """按最新消费约束裁剪名单，保留被收回作品的原始推荐依据。

    返回 (允许提交的 items 列表, 被收回记录列表)。原 selection 不被修改。
    """
    blocked = set(policy.get("blocked_items", []))
    allowed_items = []
    withdrawn = []
    for record in selection["selections"]:
        for entry in record["entries"]:
            if entry["item"] in blocked:
                withdrawn.append(
                    {
                        "user": record["user"],
                        "item": entry["item"],
                        "original_rationale": entry,
                        "policy_revision": policy["revision"],
                        "reason": "授权服务收回展出资格，已移出最终名单",
                    }
                )
            else:
                allowed_items.append(entry["item"])
    # 同一作品可能出现在多个编辑名单中，items 去重但保留稳定顺序。
    deduped = list(dict.fromkeys(allowed_items))
    return deduped, withdrawn


def build_evidence(source):
    """时序回看证据；source 应为含完整修订代次的原始 bundle。

    冻结快照折叠了历史修订链，无法逐历史日期重建当时资格，
    因此 HTTP 工作流通过 client.bundle() 取原始资料后再调用本函数。
    """
    report = run_walk_forward(source)
    return {
        "method": report["method"],
        "split_rule": report["split_rule"],
        "candidate_rule": report["candidate_rule"],
        "primary_evidence": report["primary_evidence"],
        "unexposed_segment": report["unexposed_segment"],
        "folds": report["folds"],
        "group_holdout_fold": report["group_holdout_fold"],
        "baselines": report["baselines"],
    }


def build_selection_artifact(snapshot, policy, users=None, limit=10, candidates=None, evidence=None):
    """组装选品成果：候选、裁剪结果、最新 policy 代次与离线证据。"""
    selection = build_selection(snapshot, users, limit, candidates)
    allowed_items, withdrawn = apply_latest_policy(selection, policy)
    artifact = {
        "as_of": snapshot["as_of"],
        "snapshot_id": snapshot["snapshot_id"],
        "method": SELECTION_METHOD,
        "items": allowed_items,
        "result": {
            "selection": selection,
            "withdrawn_by_policy": withdrawn,
            "submitted_count": len(allowed_items),
            "policy_revision_at_submit": policy["revision"],
        },
    }
    if evidence is not None:
        artifact["evidence_artifact_method"] = BACKTEST_METHOD
        artifact["offline_evidence"] = evidence
    return artifact


def publish_selection(client, as_of, request_key, users=None, limit=10, candidates=None,
                      include_evidence=True):
    """完整选品发布：取单 -> 重新读取最新 policy -> 裁剪 -> 提交。

    取单与提交之间若 policy 代次变化导致冲突，按最新 policy 重新裁剪后重试；
    超过固定次数仍冲突则把最后一次冲突向上抛出，由调用方决定。
    """
    snapshot = client.snapshot(as_of)
    # 回看需要完整修订链来重建历史日期视图，不能用只含最新代次的冻结快照。
    evidence = build_evidence(client.bundle()) if include_evidence else None
    policy = client.policy()
    last_conflict = None
    for _ in range(MAX_POLICY_RETRIES):
        artifact = build_selection_artifact(
            snapshot, policy, users=users, limit=limit, candidates=candidates, evidence=evidence
        )
        try:
            return client.publish(request_key, artifact, policy["revision"])
        except RemoteError as conflict:
            if conflict.status != 409:
                raise
            last_conflict = conflict
            policy = client.policy()
    raise last_conflict


def publish_backtest(client, as_of, request_key):
    """单独发布时序回看证据成果。"""
    evidence = build_evidence(client.bundle())
    snapshot = client.snapshot(as_of)
    policy = client.policy()
    artifact = {
        "as_of": as_of,
        "snapshot_id": snapshot["snapshot_id"],
        "method": BACKTEST_METHOD,
        "result": evidence,
    }
    return client.publish(request_key, artifact, policy["revision"])
