"""Diff and replay output must be human-readable and actionable."""
import json

import pytest
from sst import __version__
from sst.diff import build_structured_diff, diff_policy_snapshot, format_human_diff
from sst.governance import (
    create_baseline_from_capture,
    governance_policy_snapshot,
    save_baseline_record,
)
from sst.replay import ReplayEngine


def test_format_human_diff_contains_path_and_both_values():
    changes = build_structured_diff({"price": 100, "tax": 10}, {"price": 150, "tax": 10})
    human = format_human_diff(changes)
    assert "$.price" in human
    assert "100" in human
    assert "150" in human


def test_format_human_diff_identifies_added_key():
    changes = build_structured_diff({"a": 1}, {"a": 1, "b": 2})
    human = format_human_diff(changes)
    assert "$.b" in human
    assert "added" in human.lower() or "+" in human


def test_format_human_diff_identifies_removed_key():
    changes = build_structured_diff({"a": 1, "b": 2}, {"a": 1})
    human = format_human_diff(changes)
    assert "$.b" in human
    assert "removed" in human.lower() or "-" in human


def test_format_human_diff_reorder_hint_contains_actionable_config(tmp_path):
    changes = [
        {
            "path": "$.tags[0]",
            "change_type": "value_changed",
            "baseline": "python",
            "current": "rust",
            "severity": "medium",
        },
        {
            "path": "$.tags[1]",
            "change_type": "value_changed",
            "baseline": "rust",
            "current": "python",
            "severity": "medium",
        },
    ]
    human = format_human_diff(changes)
    assert "Hint" in human
    assert "list_sort_paths" in human or "sort_lists_paths" in human
    assert "$.tags" in human


def test_policy_drift_regression_contains_drift_in_changes(tmp_path):
    baseline_dir = tmp_path / "b"
    capture_dir = tmp_path / "c"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    capture = {
        "module": "mod",
        "function": "fn",
        "semantic_id": "drift-test",
        "output": {"raw_result": {"ok": True}},
    }
    baseline = create_baseline_from_capture(capture)
    baseline["metadata"]["diff_policy_snapshot"] = {
        **diff_policy_snapshot(),
        "hash": "stale-hash-that-does-not-match",
    }
    current = create_baseline_from_capture(capture)

    (baseline_dir / "mod.fn_drift.json").write_text(json.dumps(baseline), encoding="utf-8")
    (capture_dir / "mod.fn_drift_1.json").write_text(json.dumps(current), encoding="utf-8")

    report = ReplayEngine(str(baseline_dir), str(capture_dir)).replay()

    assert len(report["regressions"]) == 1
    reg = report["regressions"][0]
    assert reg["changes"][0]["change_type"] == "POLICY_DRIFT"
    assert reg["changes"][0]["severity"] == "high"
    assert any("POLICY_DRIFT" in w for w in report["warnings"])


def test_engine_version_warning_contains_both_version_numbers(tmp_path):
    baseline_dir = tmp_path / "b"
    capture_dir = tmp_path / "c"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    old_version = "0.1.0"
    capture = {
        "module": "mod",
        "function": "fn",
        "semantic_id": "ver-test",
        "engine_version": old_version,
        "output": {"raw_result": {"v": 1}},
    }
    baseline = create_baseline_from_capture(capture)
    baseline["scenario"]["engine_version"] = old_version
    current = create_baseline_from_capture({**capture, "engine_version": __version__})

    (baseline_dir / "mod.fn_ver.json").write_text(json.dumps(baseline), encoding="utf-8")
    (capture_dir / "mod.fn_ver_1.json").write_text(json.dumps(current), encoding="utf-8")

    report = ReplayEngine(str(baseline_dir), str(capture_dir)).replay()

    assert report["regressions"] == []
    assert report["warnings"]
    warning = report["warnings"][0]
    assert old_version in warning
    assert __version__ in warning
    assert "reinterpretation" in warning


def test_regression_error_message_contains_scenario_id_and_approve_hint(tmp_path):
    """RegressionDetectedError raised by verify mode must be actionable."""
    import os
    from unittest.mock import patch

    from sst.core import SSTCore, _Fingerprint
    from sst.errors import RegressionDetectedError
    from sst.governance import create_baseline_from_capture, save_baseline_record

    shadow = str(tmp_path / "s")
    bdir = str(tmp_path / "b")
    os.makedirs(shadow)
    os.makedirs(bdir)

    def compute(x):
        return {"result": x * 2}

    fn = compute

    with patch.dict(os.environ, {"SST_ENABLED": "true"}):
        from sst.config import refresh_config

        refresh_config()
        core = SSTCore(storage_dir=shadow, baseline_dir=bdir)
        normalizer = core._normalizer
        inp = normalizer.mask_pii(core._serialize({"args": [77], "kwargs": {}}))
        sem = _Fingerprint.semantic_hash(inp)

        fake = {
            "module": __name__,
            "function": "compute",
            "semantic_id": sem,
            "input": inp,
            "output": {"raw_result": {"result": 9999}, "status": "success"},
            "engine_version": __version__,
        }
        bp = os.path.join(bdir, f"{__name__}.compute_{sem}.json")
        save_baseline_record(bp, create_baseline_from_capture(fake))

        with patch.dict(os.environ, {"SST_VERIFY": "true"}):
            refresh_config()
            core2 = SSTCore(storage_dir=shadow, baseline_dir=bdir)
            wrapped = core2.capture(fn)
            with pytest.raises(RegressionDetectedError) as exc_info:
                wrapped(77)

    exc = exc_info.value
    err = str(exc)
    assert "REGRESSION DETECTED" in err
    assert f"{__name__}.compute" in err
    assert "sst approve" in err
    assert exc.args == (err,)
    assert exc.scenario_id == f"{__name__}.compute:{sem}"
    assert exc.error_code == "SEMANTIC_REGRESSION"
