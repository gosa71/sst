import json

import pytest

from sst.errors import BaselineValidationError, ReplayExecutionError
from sst.governance import create_baseline_from_capture
from sst.replay import ReplayEngine


@pytest.fixture
def baseline_and_capture_dirs(tmp_path):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    good = create_baseline_from_capture(
        {
            "module": "mod",
            "function": "fn",
            "semantic_id": "ok",
            "output": {"raw_result": {"value": 1}},
        }
    )
    (capture_dir / "mod.fn_ok_1.json").write_text(json.dumps(good), encoding="utf-8")
    return baseline_dir, capture_dir, good


def test_missing_required_fields_fail_validation(baseline_and_capture_dirs):
    baseline_dir, capture_dir, good = baseline_and_capture_dirs
    broken = json.loads(json.dumps(good))
    broken["scenario"].pop("module")
    (baseline_dir / "broken_missing_module.json").write_text(json.dumps(broken), encoding="utf-8")

    with pytest.raises(BaselineValidationError):
        ReplayEngine(str(baseline_dir), str(capture_dir)).replay()


def test_invalid_json_fails_loudly(baseline_and_capture_dirs):
    baseline_dir, capture_dir, _ = baseline_and_capture_dirs
    (baseline_dir / "broken.json").write_text("{this is invalid json", encoding="utf-8")

    with pytest.raises(ReplayExecutionError):
        ReplayEngine(str(baseline_dir), str(capture_dir)).replay()


def test_wrong_semantic_id_in_filename_is_rejected(tmp_path):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    baseline = create_baseline_from_capture(
        {
            "module": "mod",
            "function": "fn",
            "semantic_id": "right-id",
            "output": {"raw_result": {"value": 1}},
        }
    )
    capture = create_baseline_from_capture(
        {
            "module": "mod",
            "function": "fn",
            "semantic_id": "other-id",
            "output": {"raw_result": {"value": 1}},
        }
    )

    (baseline_dir / "mod.fn_right-id.json").write_text(json.dumps(baseline), encoding="utf-8")
    (capture_dir / "mod.fn_other-id_1.json").write_text(json.dumps(capture), encoding="utf-8")

    report = ReplayEngine(str(baseline_dir), str(capture_dir)).replay()

    assert report["missing"] == ["mod.fn:right-id"]
    assert report["regressions"] == []


def test_wrong_policy_hash_triggers_policy_drift(baseline_and_capture_dirs):
    baseline_dir, capture_dir, good = baseline_and_capture_dirs
    broken = json.loads(json.dumps(good))
    broken["metadata"]["diff_policy_snapshot"]["hash"] = "not-real"
    (baseline_dir / "wrong_hash.json").write_text(json.dumps(broken), encoding="utf-8")

    report = ReplayEngine(str(baseline_dir), str(capture_dir)).replay()

    assert len(report["regressions"]) == 1
    assert report["regressions"][0]["changes"][0]["change_type"] == "POLICY_DRIFT"
