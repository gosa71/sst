import json
import os
import subprocess
import sys
from pathlib import Path


def _run_cli(args, cwd, env):
    return subprocess.run(
        [sys.executable, "-m", "sst.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_app(app_path: Path, value: int) -> None:
    app_path.write_text(
        "\n".join(
            [
                "from sst.core import sst",
                "",
                "@sst.capture",
                "def produce():",
                f"    return {{'value': {value}}}",
                "",
                "if __name__ == '__main__':",
                "    produce()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_cli_record_verify_approve_and_diff_contract(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.sst]",
                'shadow_dir = ".sst_shadow"',
                'baseline_dir = ".sst_baseline"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    app_script = tmp_path / "app.py"
    _write_app(app_script, value=1)

    env = os.environ.copy()
    env["PYTHONPATH"] = f"/workspace/sst/src:{env.get('PYTHONPATH', '')}"
    env["PYTHONHASHSEED"] = "0"

    record_result = _run_cli(["record", str(app_script), "--clean"], cwd=tmp_path, env=env)
    assert record_result.returncode == 0, record_result.stderr
    assert "Baseline recorded:" in record_result.stdout

    _write_app(app_script, value=2)

    verify_fail = _run_cli(["verify", str(app_script), "--json"], cwd=tmp_path, env=env)
    assert verify_fail.returncode == 1, verify_fail.stderr
    payload = json.loads(verify_fail.stdout)

    normalized = {
        "summary": {
            "baseline_count": payload["summary"]["baseline_count"],
            "capture_count": payload["summary"]["capture_count"],
            "mismatch_count": payload["summary"]["mismatch_count"],
        },
        "scenarios": [
            {
                "scenario_id": payload["scenarios"][0]["scenario_id"].split(":", 1)[0] + ":<SID>",
                "status": payload["scenarios"][0]["status"],
                "has_diff": bool(payload["scenarios"][0]["diff"]),
            }
        ],
        "exit_code": payload["exit_code"],
    }
    expected = json.loads((Path(__file__).parent / "golden" / "cli_verify_regression_snapshot.json").read_text(encoding="utf-8"))
    assert normalized == expected

    run_capture = subprocess.run([sys.executable, str(app_script)], cwd=tmp_path, env={**env, "SST_ENABLED": "true"}, check=False)
    assert run_capture.returncode == 0

    scenario_id = payload["scenarios"][0]["scenario_id"]
    approve_result = _run_cli(["approve", scenario_id], cwd=tmp_path, env=env)
    assert approve_result.returncode == 0, approve_result.stderr
    assert "Baseline updated" in approve_result.stdout

    verify_pass = _run_cli(["verify", str(app_script), "--json"], cwd=tmp_path, env=env)
    assert verify_pass.returncode == 0, verify_pass.stderr

    diff_result = _run_cli(["diff"], cwd=tmp_path, env=env)
    assert diff_result.returncode != 0
    assert "No such command 'diff'" in (diff_result.stderr + diff_result.stdout)
