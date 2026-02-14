import pytest

from sst.config import refresh_config
from sst.diff import apply_diff_policy, build_structured_diff, diff_policy_snapshot, format_human_diff, normalize_for_compare, summarize_changes


def test_structured_diff_nested_changes():
    baseline = {"a": 1, "nested": {"items": [1, 2]}}
    current = {"a": 2, "nested": {"items": [1, 3]}, "extra": True}

    changes = build_structured_diff(baseline, current)

    assert any(c["path"] == "$.a" and c["change_type"] == "value_changed" for c in changes)
    assert any(c["path"] == "$.nested.items[1]" and c["change_type"] == "value_changed" for c in changes)
    assert any(c["path"] == "$.extra" and c["change_type"] == "added" for c in changes)


def test_human_and_summary_diff_rendering():
    changes = [{"path": "$.a", "change_type": "value_changed", "baseline": "old", "current": "new"}]
    assert "$.a" in format_human_diff(changes)
    assert "Detected 1 difference(s)" in summarize_changes(changes)


def test_normalize_for_compare_is_stable(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.sst.diff_policy]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    refresh_config()

    payload = {"b": 1.123456789, "a": "2024-01-01T12:00:00Z"}
    normalized = normalize_for_compare(payload)

    assert list(normalized.keys()) == ["a", "b"]
    assert normalized["a"] == "<timestamp>"
    assert normalized["b"] == 1.123457


def test_normalize_for_compare_sorts_configured_lists(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sst.diff_policy]\nlist_sort_paths=["$.items"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    refresh_config()

    payload = {"items": [{"k": 2}, {"k": 1}], "other": [2, 1]}
    normalized = normalize_for_compare(payload)

    assert normalized["items"] == [{"k": 1}, {"k": 2}]
    assert normalized["other"] == [2, 1]


def test_normalize_for_compare_collapses_whitespace_and_masks_uuid_optionally(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.sst.diff_policy]\nmask_uuid_like=true\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    refresh_config()

    payload = {
        "message": "  hello\n   world  ",
        "trace": "123e4567-e89b-12d3-a456-426614174000",
    }
    normalized = normalize_for_compare(payload)

    assert normalized["message"] == "hello world"
    assert normalized["trace"] == "<uuid>"


def test_apply_diff_policy_respects_ignored_paths(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.sst.diff_policy]\nignored_paths=["$.nested.keep","arr[0]"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    refresh_config()

    filtered = apply_diff_policy({"nested": {"keep": 1, "stay": 2}, "arr": [1, 2]})

    assert filtered == {"nested": {"stay": 2}, "arr": [2]}


def test_structured_diff_rejects_excessive_depth():
    baseline = current = 0
    for _ in range(1002):
        baseline = {"x": baseline}
        current = {"x": current}

    with pytest.raises(ValueError, match="Maximum diff depth exceeded"):
        build_structured_diff(baseline, current)


def test_human_diff_reorder_hint_handles_unhashable_values():
    changes = [
        {"path": "$.items[0]", "change_type": "value_changed", "baseline": {"id": 1}, "current": {"id": 2}},
        {"path": "$.items[1]", "change_type": "value_changed", "baseline": {"id": 2}, "current": {"id": 1}},
    ]

    text = format_human_diff(changes)

    assert "Hint: values are identical but order differs" in text
    assert "list_sort_paths" in text


def test_diff_policy_snapshot_is_stable(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.sst.diff_policy]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    refresh_config()

    snap_a = diff_policy_snapshot()
    snap_b = diff_policy_snapshot()

    assert snap_a["semantics_version"] == 1
    assert snap_a["hash"] == snap_b["hash"]


def test_normalize_for_compare_float_tolerance_zero_keeps_exact_float(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.sst.diff_policy]\nfloat_tolerance=0\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    refresh_config()

    value = 1.123456789123
    normalized = normalize_for_compare({"value": value})

    assert normalized["value"] == value
