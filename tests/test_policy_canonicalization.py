"""Policy snapshot hashes must be stable regardless of internal key ordering."""
from sst.diff import diff_policy_snapshot
from sst.governance import governance_policy_snapshot


def test_diff_policy_snapshot_hash_is_stable_across_calls():
    s1 = diff_policy_snapshot()
    s2 = diff_policy_snapshot()
    s3 = diff_policy_snapshot()
    assert s1["hash"] == s2["hash"] == s3["hash"]


def test_governance_policy_snapshot_hash_is_stable_across_calls():
    g1 = governance_policy_snapshot()
    g2 = governance_policy_snapshot()
    g3 = governance_policy_snapshot()
    assert g1["hash"] == g2["hash"] == g3["hash"]


def test_diff_policy_snapshot_hash_changes_when_ignored_fields_change(tmp_path, monkeypatch):
    """Adding a new ignored field must produce a different snapshot hash."""
    from sst.config import refresh_config

    (tmp_path / "pyproject.toml").write_text(
        "[tool.sst.diff_policy]\nignored_fields=[]\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    refresh_config()
    hash_empty = diff_policy_snapshot()["hash"]

    (tmp_path / "pyproject.toml").write_text(
        '[tool.sst.diff_policy]\nignored_fields=["custom_field"]\n', encoding="utf-8"
    )
    refresh_config()
    hash_with_field = diff_policy_snapshot()["hash"]

    assert hash_empty != hash_with_field
    refresh_config()


def test_governance_snapshot_contains_all_default_transitions():
    snap = governance_policy_snapshot()
    pairs = {(t["action"], t["from"]) for t in snap["transitions"]}
    expected = {
        ("approve", "pending"),
        ("approve", "approved"),
        ("approve", "deprecated"),
        ("deprecate", "approved"),
        ("deprecate", "pending"),
        ("deprecate", "deprecated"),
    }
    assert expected == pairs
