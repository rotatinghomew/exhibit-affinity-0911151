from pathlib import Path
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from exhibit_affinity.client import ProductClient
from exhibit_affinity.data import load_bundle
from exhibit_affinity.service import running_service
from exhibit_affinity.store import ArtifactStore
from exhibit_affinity.workflow import (
    publish_overview,
    publish_personalized_selection,
    publish_temporal_backtest,
)


def main():
    root = Path(__file__).resolve().parents[1]
    bundle = load_bundle(root / "fixtures/bundle.json")
    with tempfile.TemporaryDirectory() as directory:
        store = ArtifactStore(str(Path(directory) / "state.sqlite3"), bundle.get("policy"))
        try:
            with running_service(bundle, store) as base:
                client = ProductClient(base)

                overview = publish_overview(client, "2026-01-01", "overview-demo")
                backtest = publish_temporal_backtest(client, "2026-01-01", "backtest-demo")
                selection = publish_personalized_selection(
                    client,
                    "2026-01-01",
                    "selection-demo",
                    users=["editor-00", "editor-01"],
                    limit=6,
                )

                evidence = selection["artifact"]["offline_evidence"]
                fold_summary = [
                    {
                        "train_as_of": fold["train_as_of"],
                        "test_slates": fold["test_slates"],
                        "evaluated_units": fold["evaluated_units"],
                        "personalized_ndcg": fold["metrics"]["personalized-candidates-v1"]["ndcg_at_k"],
                        "popularity_ndcg": fold["metrics"]["global-popularity"]["ndcg_at_k"],
                    }
                    for fold in evidence["folds"]
                ]
                demo_output = {
                    "产品": bundle["product"],
                    "概览成果": overview["artifact_id"],
                    "回看成果": backtest["artifact_id"],
                    "选品成果": selection["artifact_id"],
                    "回看切分": evidence["split_rule"],
                    "候选口径": evidence["candidate_rule"],
                    "逐折比较": fold_summary,
                    "整组留出折": evidence["group_holdout_fold"]["group_holdout"],
                    "无曝光段": evidence["unexposed_segment"],
                    "最终提交作品": selection["artifact"]["items"],
                    "授权收回记录": selection["artifact"]["result"]["withdrawn_by_policy"],
                    "提交时代次": selection["artifact"]["result"]["policy_revision_at_submit"],
                }
                print(json.dumps(demo_output, ensure_ascii=False, indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
