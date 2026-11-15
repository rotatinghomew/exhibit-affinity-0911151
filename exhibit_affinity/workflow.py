from __future__ import annotations

from .selection import publish_backtest, publish_selection
from .product import summarize


def publish_overview(client, as_of, request_key):
    snapshot = client.snapshot(as_of)
    policy = client.policy()
    artifact = {
        "as_of": as_of,
        "snapshot_id": snapshot["snapshot_id"],
        "method": "observed-overview-v1",
        "result": summarize(snapshot),
    }
    return client.publish(request_key, artifact, policy["revision"])


def publish_personalized_selection(client, as_of, request_key, users=None, limit=10,
                                   candidates=None, include_evidence=True):
    """发布个性化选品成果，离线证据与最新资格裁剪在同一工作流内完成。"""
    return publish_selection(
        client,
        as_of,
        request_key,
        users=users,
        limit=limit,
        candidates=candidates,
        include_evidence=include_evidence,
    )


def publish_temporal_backtest(client, as_of, request_key):
    """单独发布按时间回看的策略比较证据。"""
    return publish_backtest(client, as_of, request_key)
