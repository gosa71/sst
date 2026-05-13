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


def test_config_defaults_include_new_polish_flags(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.sst]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cfg = refresh_config()

    assert cfg.clean_shadow_on_record is False
    assert cfg.strict_pii_matching is True
    assert cfg.max_capture_dir_bytes == 1 * 1024 * 1024 * 1024
    assert cfg.max_pending_files == 10000


def test_config_loads_new_polish_flags_from_pyproject(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.sst]\nclean_shadow_on_record=true\nstrict_pii_matching=false\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    cfg = refresh_config()

    assert cfg.clean_shadow_on_record is True
    assert cfg.strict_pii_matching is False


def test_refresh_config_picks_up_env_change(monkeypatch, tmp_path):
    from sst.config import get_config, load_config, refresh_config

    load_config.cache_clear()
    monkeypatch.setenv("SST_BASELINE_DIR", str(tmp_path / "v1"))
    c1 = get_config()
    assert "v1" in c1.baseline_dir

    monkeypatch.setenv("SST_BASELINE_DIR", str(tmp_path / "v2"))
    c2_cached = get_config()
    assert "v1" in c2_cached.baseline_dir

    c2_fresh = refresh_config()
    assert "v2" in c2_fresh.baseline_dir
