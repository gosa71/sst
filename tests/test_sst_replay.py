import json

import pytest

from sst.config import refresh_config
from sst.errors import BaselineValidationError, ReplayExecutionError
from sst.governance import create_baseline_from_capture
from sst.replay import ReplayEngine


def test_replay_detects_regression(tmp_path):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    baseline_capture = {
        "module": "mod",
        "function": "fn",
        "semantic_id": "id1",
        "output": {"raw_result": {"value": 1}},
    }
    current_capture = {
        "module": "mod",
        "function": "fn",
        "semantic_id": "id1",
        "output": {"raw_result": {"value": 2}},
    }

    (baseline_dir / "mod.fn_id1.json").write_text(json.dumps(create_baseline_from_capture(baseline_capture)))
    (capture_dir / "mod.fn_id1_1.json").write_text(json.dumps(create_baseline_from_capture(current_capture)))

    report = ReplayEngine(str(baseline_dir), str(capture_dir)).replay()
    assert len(report["regressions"]) == 1
    assert report["regressions"][0]["scenario_id"] == "mod.fn:id1"


def test_replay_rejects_duplicate_baseline_scenarios_deterministically(tmp_path):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    scenario = {
        "module": "mod",
        "function": "fn",
        "semantic_id": "dup",
        "output": {"raw_result": {"value": 1}},
    }

    (baseline_dir / "b.json").write_text(json.dumps(create_baseline_from_capture(scenario)))
    (baseline_dir / "a.json").write_text(json.dumps(create_baseline_from_capture(scenario)))

    with pytest.raises(ReplayExecutionError, match=r"Duplicate scenario key detected: mod\.fn:dup\. Files: a\.json and b\.json"):
        ReplayEngine(str(baseline_dir), str(capture_dir)).replay()


def test_replay_rejects_missing_required_baseline_field(tmp_path):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    invalid = create_baseline_from_capture({
        "module": "mod",
        "function": "fn",
        "semantic_id": "id1",
        "output": {"raw_result": {"value": 1}},
    })
    invalid["scenario"].pop("module")

    (baseline_dir / "bad.json").write_text(json.dumps(invalid))

    with pytest.raises(BaselineValidationError, match="Baseline scenario missing required field 'module'"):
        ReplayEngine(str(baseline_dir), str(capture_dir)).replay()


def test_replay_normalize_output_uses_configured_normalization(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sst.diff_policy]\nlist_sort_paths=["$.items"]\nnormalize_string_whitespace=true\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    refresh_config()

    engine = ReplayEngine(str(tmp_path / "baseline"), str(tmp_path / "capture"))
    normalized = engine.normalize_output({"items": [" b", "a  "]})

    assert normalized == {"items": ["a", "b"]}


def test_replay_policy_drift_is_reported_as_regression(tmp_path, monkeypatch):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    baseline_capture = {
        "module": "mod",
        "function": "fn",
        "semantic_id": "id1",
        "engine_version": "0.1.0",
        "output": {"raw_result": {"value": 1}},
    }
    current_capture = {
        "module": "mod",
        "function": "fn",
        "semantic_id": "id1",
        "output": {"raw_result": {"value": 1}},
    }

    baseline_record = create_baseline_from_capture(baseline_capture)
    baseline_record["metadata"]["diff_policy_snapshot"]["hash"] = "changed"

    (baseline_dir / "mod.fn_id1.json").write_text(json.dumps(baseline_record))
    (capture_dir / "mod.fn_id1_1.json").write_text(json.dumps(create_baseline_from_capture(current_capture)))

    report = ReplayEngine(str(baseline_dir), str(capture_dir)).replay()

    assert len(report["regressions"]) == 1
    assert report["regressions"][0]["changes"][0]["change_type"] == "POLICY_DRIFT"
    assert any("potential reinterpretation risk" in warning for warning in report["warnings"])


def test_replay_missing_new_metadata_is_advisory_warning(tmp_path):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    baseline_capture = {
        "module": "mod",
        "function": "fn",
        "semantic_id": "id1",
        "output": {"raw_result": {"value": 1}},
    }

    baseline_record = create_baseline_from_capture(baseline_capture)
    baseline_record["scenario"].pop("engine_version", None)
    baseline_record["metadata"].pop("diff_policy_snapshot", None)
    baseline_record["metadata"].pop("governance_policy_snapshot", None)

    (baseline_dir / "mod.fn_id1.json").write_text(json.dumps(baseline_record))
    (capture_dir / "mod.fn_id1_1.json").write_text(json.dumps(create_baseline_from_capture(baseline_capture)))

    report = ReplayEngine(str(baseline_dir), str(capture_dir)).replay()

    assert report["regressions"] == []
    assert any("missing engine_version" in warning for warning in report["warnings"])
    assert any("missing diff_policy_snapshot" in warning for warning in report["warnings"])
