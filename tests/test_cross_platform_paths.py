"""Scenario identity parsing must handle underscores in module/function names."""
from sst.governance import _parse_scenario_identity_from_path


PARSE_CASES = [
    # (filename_stem, expected_module, expected_function, expected_semantic_id)
    (
        "myapp.billing.calculate_abc123def456789012345678901234",
        "myapp.billing",
        "calculate",
        "abc123def456789012345678901234",
    ),
    (
        "my_app.billing.calculate_abc123def456789012345678901234",
        "my_app.billing",
        "calculate",
        "abc123def456789012345678901234",
    ),
    (
        "app.mod.fn_with_underscore_abc123def456789012345678901234",
        "app.mod",
        "fn_with_underscore",
        "abc123def456789012345678901234",
    ),
]


def test_parse_normal_baseline_filename():
    result = _parse_scenario_identity_from_path(
        "myapp.billing.calculate_abc123def456789012345678901234.json"
    )
    assert result["module"] == "myapp.billing"
    assert result["function"] == "calculate"
    assert result["semantic_id"] == "abc123def456789012345678901234"


def test_parse_module_with_underscore():
    result = _parse_scenario_identity_from_path(
        "my_app.billing.calculate_abc123def456789012345678901234.json"
    )
    assert result["module"] == "my_app.billing"
    assert result["function"] == "calculate"


def test_parse_function_with_underscore():
    result = _parse_scenario_identity_from_path(
        "app.mod.fn_with_underscore_abc123def456789012345678901234.json"
    )
    assert result["function"] == "fn_with_underscore"
    assert result["semantic_id"] == "abc123def456789012345678901234"


def test_parse_returns_empty_for_no_dot():
    result = _parse_scenario_identity_from_path("nodot_abc123.json")
    assert result == {}


def test_parse_returns_empty_for_no_underscore():
    result = _parse_scenario_identity_from_path("module.function.json")
    assert result == {}


def test_parse_identity_is_not_used_when_json_has_fields(tmp_path):
    """_upgrade_legacy_record must NOT overwrite existing fields with parsed values."""
    from sst.governance import _upgrade_legacy_record

    # Full record (scenario + metadata) — existing fields must not be overwritten
    data = {
        "scenario": {
            "module": "correct.module",
            "function": "correct_fn",
            "semantic_id": "correct_semantic_id",
            "input": {},
            "output": {},
        },
        "metadata": {"format_version": 1},
        "approval_history": [],
    }
    # Filename that would parse to different values if used
    result = _upgrade_legacy_record(data, "wrong.module.wrong_fn_wrongsemantic.json")
    assert result["scenario"]["module"] == "correct.module"
    assert result["scenario"]["function"] == "correct_fn"
    assert result["scenario"]["semantic_id"] == "correct_semantic_id"
