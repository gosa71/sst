import json

from sst.diff import diff_policy_snapshot
from sst.governance import create_baseline_from_capture, governance_policy_snapshot
from sst.replay import ReplayEngine


def test_policy_change_triggers_regression(tmp_path):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    capture = {
        "module": "pkg",
        "function": "policy_target",
        "semantic_id": "sid-1",
        "output": {"raw_result": {"ok": True}},
    }

    baseline = create_baseline_from_capture(capture)
    baseline["metadata"]["diff_policy_snapshot"] = {
        **diff_policy_snapshot(),
        "hash": "old-diff-policy-hash",
    }
    baseline["metadata"]["governance_policy_snapshot"] = {
        **governance_policy_snapshot(),
        "hash": "old-governance-policy-hash",
    }

    current = create_baseline_from_capture(capture)

    (baseline_dir / "pkg.policy_target_sid-1.json").write_text(json.dumps(baseline), encoding="utf-8")
    (capture_dir / "pkg.policy_target_sid-1_1.json").write_text(json.dumps(current), encoding="utf-8")

    report = ReplayEngine(str(baseline_dir), str(capture_dir)).replay()

    assert len(report["regressions"]) == 1
    regression = report["regressions"][0]
    assert regression["scenario_id"] == "pkg.policy_target:sid-1"
    assert regression["changes"][0]["change_type"] == "POLICY_DRIFT"
    assert regression["changes"][0]["severity"] == "high"
    assert any("POLICY_DRIFT" in warning for warning in report["warnings"])
