import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from sst import cli as sst_cli

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

APP_TEMPLATE = """from sst.core import sst

@sst.capture
def calc(x):
    return {{\"value\": x * {multiplier}}}

if __name__ == \"__main__\":
    calc(2)
"""


def _write_app(path: Path, multiplier: int) -> None:
    path.write_text(APP_TEMPLATE.format(multiplier=multiplier), encoding="utf-8")


def _first_baseline_file(baseline_dir: Path) -> Path:
    files = sorted(baseline_dir.glob("*.json"))
    assert files, "Expected at least one baseline file"
    return files[0]


def _scenario_id_from_baseline(baseline_path: Path) -> str:
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    scenario = payload["scenario"]
    return f"{scenario['module']}.{scenario['function']}:{scenario['semantic_id']}"


def _invoke(runner: CliRunner, args: list[str]):
    return runner.invoke(sst_cli.main, args, catch_exceptions=False)


def test_full_cli_workflow_emulation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))

    app_script = tmp_path / "app.py"
    _write_app(app_script, multiplier=1)

    runner = CliRunner()

    # 1) Record baseline
    record_result = _invoke(runner, ["record", str(app_script)])
    assert record_result.exit_code == 0

    baseline_dir = tmp_path / ".sst_baseline"
    assert baseline_dir.exists()
    baseline_file = _first_baseline_file(baseline_dir)
    scenario_id = _scenario_id_from_baseline(baseline_file)

    # 2) Governance read paths
    list_result = _invoke(runner, ["baseline", "list"])
    assert list_result.exit_code == 0
    assert scenario_id in list_result.output

    show_result = _invoke(runner, ["baseline", "show", scenario_id])
    assert show_result.exit_code == 0
    shown = json.loads(show_result.output)
    assert shown["scenario"]["semantic_id"] == scenario_id.split(":", 1)[1]

    # 3) Introduce regression and verify it is detected
    _write_app(app_script, multiplier=2)
    verify_fail_result = _invoke(runner, ["verify", str(app_script)])
    assert verify_fail_result.exit_code == 1
    assert "FAIL:" in verify_fail_result.output

    # 4) Capture new behavior and approve
    capture_result = subprocess.run(
        [sys.executable, str(app_script)],
        env={**os.environ, "SST_ENABLED": "true"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert capture_result.returncode == 0

    approve_result = _invoke(runner, ["approve", scenario_id])
    assert approve_result.exit_code == 0

    # 5) Verify pass path + governance mutation + cleanup
    verify_pass_result = _invoke(runner, ["verify", str(app_script), "--json"])
    assert verify_pass_result.exit_code == 0
    payload = json.loads(verify_pass_result.output)
    assert payload["scenarios"][0]["status"] == "pass"

    deprecate_result = _invoke(runner, ["baseline", "deprecate", scenario_id])
    assert deprecate_result.exit_code == 0

    clean_result = _invoke(runner, ["clean"])
    assert clean_result.exit_code == 0
    assert not (tmp_path / ".shadow_data").exists()


def test_generate_command_emulates_all_generation_inputs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shadow_data = tmp_path / ".shadow_data"
    shadow_data.mkdir()
    (shadow_data / "capture.json").write_text("{}", encoding="utf-8")

    calls = {}

    class FakeSynth:
        def run(self, func_filter, output_dir, open_editor):
            calls["func_filter"] = func_filter
            calls["output_dir"] = output_dir
            calls["open_editor"] = open_editor

    monkeypatch.setattr(sst_cli, "SSTSynthesizer", lambda: FakeSynth())

    runner = CliRunner()
    original_provider = os.environ.get("SST_PROVIDER")
    original_model = os.environ.get("SST_MODEL")

    with patch.dict(os.environ, dict(os.environ), clear=True):
        result = _invoke(
            runner,
            [
                "generate",
                "--all",
                "--output-dir",
                "gen_tests",
                "--provider",
                "openai",
                "--model",
                "gpt-4o-mini",
                "--edit",
            ],
        )

        assert result.exit_code == 0
        assert os.environ["SST_PROVIDER"] == "openai"
        assert os.environ["SST_MODEL"] == "gpt-4o-mini"

    assert os.environ.get("SST_PROVIDER") == original_provider
    assert os.environ.get("SST_MODEL") == original_model
    assert calls == {"func_filter": None, "output_dir": "gen_tests", "open_editor": True}
