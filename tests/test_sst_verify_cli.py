import json
from types import SimpleNamespace

from click.testing import CliRunner

from sst import cli as sst_cli


def _write_script(path):
    path.write_text("print('ok')\n", encoding="utf-8")


def _write_baseline(path):
    baseline_dir = path / ".sst_baseline"
    baseline_dir.mkdir()
    baseline_file = baseline_dir / "mod.fn_id1.json"
    baseline_file.write_text(
        json.dumps(
            {
                "scenario": {
                    "module": "mod",
                    "function": "fn",
                    "semantic_id": "id1",
                    "output": {"raw_result": {"value": 1}},
                },
                "metadata": {"version_id": "v1", "scenario_status": "approved"},
            }
        ),
        encoding="utf-8",
    )


def _fake_regression_report():
    return {
        "baseline_count": 1,
        "capture_count": 1,
        "missing": [],
        "regressions": [
            {
                "scenario_id": "mod.fn:id1",
                "status": "failed",
                "summary": "Detected 1 difference(s)",
                "human_diff": "~ $.value: 1 -> 2",
                "changes": [
                    {"path": "$.value", "baseline": 1, "current": 2, "change_type": "value_changed"}
                ],
                "baseline_version": "v1",
            }
        ],
        "scenarios": [
            {
                "scenario_id": "mod.fn:id1",
                "status": "failed",
                "summary": "Detected 1 difference(s)",
                "human_diff": "~ $.value: 1 -> 2",
                "changes": [
                    {"path": "$.value", "baseline": 1, "current": 2, "change_type": "value_changed"}
                ],
                "baseline_version": "v1",
            }
        ],
    }


def test_verify_human_report_and_exit_code_for_regression(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_baseline(tmp_path)
    app_script = tmp_path / "app.py"
    _write_script(app_script)

    monkeypatch.setattr(sst_cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""))

    class FakeEngine:
        def __init__(self, baseline_dir, capture_dir):
            self.baseline_dir = baseline_dir
            self.capture_dir = capture_dir

        def replay(self):
            return _fake_regression_report()

    monkeypatch.setattr(sst_cli, "ReplayEngine", FakeEngine)

    runner = CliRunner()
    result = runner.invoke(sst_cli.main, ["verify", str(app_script)], catch_exceptions=False)

    assert result.exit_code == 1
    assert "SST Verification Report" in result.output
    assert "FAIL: mod.fn:id1" in result.output
    assert "Summary: Detected 1 difference(s)" in result.output
    assert "Baseline version: v1" in result.output
    assert "~ $.value: 1 -> 2" in result.output
    assert "To approve intentional changes" in result.output


def test_verify_verbose_prints_full_diff(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_baseline(tmp_path)
    app_script = tmp_path / "app.py"
    _write_script(app_script)

    monkeypatch.setattr(sst_cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""))

    class FakeEngine:
        def __init__(self, baseline_dir, capture_dir):
            self.baseline_dir = baseline_dir
            self.capture_dir = capture_dir

        def replay(self):
            return _fake_regression_report()

    monkeypatch.setattr(sst_cli, "ReplayEngine", FakeEngine)

    runner = CliRunner()
    result = runner.invoke(sst_cli.main, ["verify", str(app_script), "--verbose"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "~ $.value: 1 -> 2" in result.output


def test_verify_json_output_is_deterministic(tmp_path):
    report = {
        "baseline_count": 2,
        "capture_count": 2,
        "missing": [],
        "regressions": [],
        "scenarios": [
            {"scenario_id": "b", "status": "passed", "changes": [], "baseline_version": "2"},
            {"scenario_id": "a", "status": "failed", "changes": [{"path": "$.x"}], "baseline_version": "1"},
        ],
    }

    output = sst_cli._build_ci_json_report(report)

    assert output["summary"]["sst_version"] == sst_cli.VERSION
    assert [row["scenario_id"] for row in output["scenarios"]] == ["a", "b"]
    assert output["scenarios"][0]["status"] == "fail"
    assert output["exit_code"] == 1


def test_verify_json_command_outputs_only_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_baseline(tmp_path)
    app_script = tmp_path / "app.py"
    _write_script(app_script)

    monkeypatch.setattr(sst_cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""))

    class FakeEngine:
        def __init__(self, baseline_dir, capture_dir):
            self.baseline_dir = baseline_dir
            self.capture_dir = capture_dir

        def replay(self):
            return {
                "baseline_count": 1,
                "capture_count": 1,
                "missing": [],
                "regressions": [],
                "scenarios": [
                    {
                        "scenario_id": "mod.fn:id1",
                        "status": "passed",
                        "summary": "No semantic differences detected.",
                        "human_diff": "",
                        "changes": [],
                        "baseline_version": "v1",
                    }
                ],
            }

    monkeypatch.setattr(sst_cli, "ReplayEngine", FakeEngine)

    runner = CliRunner()
    result = runner.invoke(sst_cli.main, ["verify", str(app_script), "--json"], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["summary"]["sst_version"] == sst_cli.VERSION
    assert payload["scenarios"][0]["status"] == "pass"
    assert payload["exit_code"] == 0


def test_verify_exit_code_2_on_internal_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_baseline(tmp_path)
    app_script = tmp_path / "app.py"
    _write_script(app_script)

    monkeypatch.setattr(sst_cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=3, stderr="boom"))

    runner = CliRunner()
    result = runner.invoke(sst_cli.main, ["verify", str(app_script)], catch_exceptions=False)

    assert result.exit_code == 2
    assert "SST error [SYSTEM:VERIFY_REPLAY_CAPTURE_FAILED]" in result.output
