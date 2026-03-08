import json
import pytest

from sst.config import refresh_config
from sst.errors import BaselineValidationError, GovernancePolicyError
from sst.errors import BaselineFormatError
from sst.diff import diff_policy_snapshot
from sst.governance import (
    approve_scenario,
    create_baseline_from_capture,
    deprecate_scenario,
    evaluate_governance_decision,
    list_scenarios,
    load_baseline_record,
    set_custom_transition_validator,
)


def test_create_and_load_governed_baseline(tmp_path):
    capture = {"module": "m", "function": "f", "semantic_id": "abc", "output": {"raw_result": {"x": 1}}}
    record = create_baseline_from_capture(capture)
    path = tmp_path / "m.f_abc.json"
    path.write_text(json.dumps(record))

    loaded = load_baseline_record(str(path))
    assert loaded["metadata"]["scenario_status"] == "approved"
    assert loaded["scenario"]["semantic_id"] == "abc"


def test_approve_and_deprecate_updates_history(tmp_path):
    path = tmp_path / "m.f_abc.json"
    capture = {"module": "m", "function": "f", "semantic_id": "abc", "output": {"raw_result": {"x": 1}}}

    approve_scenario(str(path), capture)
    updated = deprecate_scenario(str(path))

    assert updated["metadata"]["scenario_status"] == "deprecated"
    assert len(updated["approval_history"]) >= 2


def test_reapprove_refreshes_policy_snapshots(tmp_path):
    path = tmp_path / "m.f_abc.json"
    capture = {"module": "m", "function": "f", "semantic_id": "abc", "output": {"raw_result": {"x": 1}}}

    approve_scenario(str(path), capture)
    original = load_baseline_record(str(path))

    original["metadata"]["diff_policy_snapshot"] = {"hash": "stale", "semantics_version": 1}
    original["metadata"]["governance_policy_snapshot"] = {"hash": "stale"}
    path.write_text(json.dumps(original), encoding="utf-8")

    updated = approve_scenario(str(path), capture)

    assert updated["metadata"]["diff_policy_snapshot"]["hash"] == diff_policy_snapshot()["hash"]
    assert updated["metadata"]["governance_policy_snapshot"]["hash"] != "stale"


def test_governance_decision_is_explainable():
    decision = evaluate_governance_decision("deprecate", "approved")

    assert decision.allowed is True
    assert decision.reason_code == "DEPRECATE_ALLOWED"
    assert "deprecated" in decision.explanation.lower()


def test_load_legacy_baseline_adds_format_version(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"module": "m", "function": "f", "semantic_id": "abc"}))

    loaded = load_baseline_record(str(path))
    assert loaded["scenario"]["module"] == "m"
    assert loaded["metadata"]["format_version"] == 1


def test_load_baseline_record_rejects_oversized_file(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[tool.sst]\nmax_baseline_size = 10\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    refresh_config()

    path = tmp_path / "large.json"
    path.write_text('{"scenario": {"module": "m", "function": "f", "semantic_id": "x"}, "metadata": {"format_version": 1}}')

    with pytest.raises(BaselineFormatError, match="Baseline file exceeds maximum allowed size"):
        load_baseline_record(str(path))


def test_governance_invalid_transition_raises_value_error():
    with pytest.raises(GovernancePolicyError, match="Invalid governance transition: archive -> approved"):
        evaluate_governance_decision("archive", "approved")


def test_invalid_baseline_type_detection(tmp_path):
    path = tmp_path / "invalid_baseline.json"
    payload = {
        "scenario": {
            "module": "m",
            "function": "f",
            "semantic_id": "abc",
            "input": [],
            "output": {},
        },
        "metadata": {"format_version": 1},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BaselineValidationError, match="field 'input': expected dict"):
        load_baseline_record(str(path))


def test_custom_governance_transition_hook_allows_unknown_transition():
    set_custom_transition_validator(lambda old_state, new_state: old_state == "approved" and new_state == "archive")
    try:
        decision = evaluate_governance_decision("archive", "approved")
    finally:
        set_custom_transition_validator(None)

    assert decision.allowed is True
    assert decision.reason_code == "CUSTOM_TRANSITION_ALLOWED"


def test_strict_governance_mode_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SST_STRICT_GOVERNANCE", "false")
    refresh_config()

    decision = evaluate_governance_decision("archive", "approved")

    assert decision.allowed is True
    assert decision.reason_code == "STRICT_GOVERNANCE_DISABLED"
    refresh_config()


def test_list_scenarios_skips_corrupted_file(tmp_path):
    good = create_baseline_from_capture(
        {
            "module": "m",
            "function": "fn",
            "semantic_id": "a" * 32,
            "input": {},
            "output": {"raw_result": 1},
        }
    )
    save_path = tmp_path / f"m.fn_{'a' * 32}.json"
    save_path.write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / f"bad_{'b' * 32}.json").write_text("{bad json", encoding="utf-8")

    rows = list_scenarios(str(tmp_path))
    real_rows = [r for r in rows if r["scenario_id"] is not None]
    warn_rows = [r for r in rows if r.get("_warning")]
    assert len(real_rows) == 1
    assert len(warn_rows) == 1


def test_list_scenarios_all_corrupted_returns_warnings(tmp_path):
    (tmp_path / f"bad1_{'a' * 32}.json").write_text("{", encoding="utf-8")
    (tmp_path / f"bad2_{'b' * 32}.json").write_text("null", encoding="utf-8")

    rows = list_scenarios(str(tmp_path))
    assert [r for r in rows if r["scenario_id"] is not None] == []
    assert len([r for r in rows if r.get("_warning")]) == 2


def test_deprecate_updates_version_id(tmp_path):
    cap = {"module": "m", "function": "fn", "semantic_id": "a" * 32, "input": {}, "output": {"raw_result": 1}}
    record = create_baseline_from_capture(cap)
    record["metadata"]["scenario_status"] = "approved"
    path = tmp_path / f"m.fn_{'a' * 32}.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    original_vid = record["metadata"]["version_id"]
    updated = deprecate_scenario(str(path))

    assert updated["metadata"]["version_id"] != original_vid
    assert updated["metadata"]["scenario_status"] == "deprecated"


def test_deprecate_version_id_is_uuid(tmp_path):
    import uuid

    cap = {"module": "m", "function": "fn", "semantic_id": "b" * 32, "input": {}, "output": {"raw_result": 2}}
    record = create_baseline_from_capture(cap)
    record["metadata"]["scenario_status"] = "approved"
    path = tmp_path / f"m.fn_{'b' * 32}.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    updated = deprecate_scenario(str(path))
    uuid.UUID(updated["metadata"]["version_id"])
