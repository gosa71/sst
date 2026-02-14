import textwrap

from sst.config import refresh_config
from sst.diff import apply_diff_policy, normalize_for_compare


def test_config_loads_from_pyproject(tmp_path, monkeypatch):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        textwrap.dedent(
            """
            [tool.sst]
            baseline_dir = "custom-baseline"
            shadow_dir = "custom-shadow"
            sampling_rate = 0.5
            pii_keys = ["session_token"]
            governance_policy = "default"

            [tool.sst.diff_policy]
            ignored_fields = ["event_id"]
            float_tolerance = 0.001
            """
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    cfg = refresh_config()

    assert cfg.baseline_dir == "custom-baseline"
    assert cfg.shadow_dir == "custom-shadow"
    assert cfg.sampling_rate == 0.5
    assert cfg.pii_keys == ["session_token"]
    assert cfg.diff_policy["ignored_fields"] == ["event_id"]


def test_env_override_sampling_rate(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.sst]\nsampling_rate=0.9\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SST_SAMPLING_RATE", "0.25")

    cfg = refresh_config()
    assert cfg.sampling_rate == 0.25


def test_diff_policy_uses_configured_ignored_fields(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.sst.diff_policy]\nignored_fields=[\"volatile\"]\nfloat_tolerance=0.01\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    refresh_config()

    filtered = apply_diff_policy({"volatile": "x", "stable": 1})
    normalized = normalize_for_compare({"value": 3.14159})

    assert filtered == {"stable": 1}
    assert normalized["value"] == 3.14
