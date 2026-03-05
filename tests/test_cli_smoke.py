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


class TestBaselineDeprecateOrphaned:
    """Tests for `sst baseline deprecate --orphaned`."""

    def _run(self, args, shadow_dir, baseline_dir, cwd):
        import subprocess
        import sys

        env = {
            **__import__("os").environ,
            "PYTHONPATH": "src",
            "SST_ENABLED": "true",
            "SST_SHADOW_DIR": str(shadow_dir),
            "SST_BASELINE_DIR": str(baseline_dir),
        }
        return subprocess.run(
            [sys.executable, "-m", "sst.cli"] + args,
            env=env,
            capture_output=True,
            text=True,
            cwd=str(cwd),
        )

    def test_orphaned_empty_shadow_is_error(self, tmp_path):
        """--orphaned with empty shadow_data exits 2 with helpful message."""
        shadow = tmp_path / "shadow"
        baseline = tmp_path / "baseline"
        shadow.mkdir()
        baseline.mkdir()
        root = __import__("pathlib").Path(__file__).parent.parent

        # Record a baseline, then wipe shadow to simulate post-refactor
        r = self._run(["record", "pricing.py"], shadow, baseline, root)
        assert r.returncode == 0
        import shutil

        shutil.rmtree(shadow)
        shadow.mkdir()  # now empty

        r = self._run(["baseline", "deprecate", "--orphaned"], shadow, baseline, root)
        assert r.returncode == 2
        assert "record" in r.stdout.lower() or "empty" in r.stdout.lower()

    def test_orphaned_dry_run_lists_candidates(self, tmp_path):
        """--orphaned without --apply prints candidates and does not deprecate."""
        shadow = tmp_path / "shadow"
        baseline = tmp_path / "baseline"
        shadow.mkdir()
        baseline.mkdir()
        root = __import__("pathlib").Path(__file__).parent.parent
        import json as _json
        import shutil

        # Record baseline (4 scenarios, shadow matches baseline)
        r = self._run(["record", "pricing.py"], shadow, baseline, root)
        assert r.returncode == 0

        # Replace all captures with a dummy that matches nothing in baseline
        shutil.rmtree(shadow)
        shadow.mkdir()
        dummy = shadow / "other.fn_abcdef1234567890abcdef1234567890_123456_1.json"
        dummy.write_text(
            _json.dumps(
                {
                    "function": "fn",
                    "module": "other",
                    "semantic_id": "abcdef1234567890abcdef1234567890",
                    "engine_version": "0.2.0",
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "input": {},
                    "output": {},
                    "dependencies": [],
                    "execution_metadata": {},
                    "dependency_capture": {},
                    "source": "",
                }
            ),
            encoding="utf-8",
        )

        # Dry run — should list 4 orphans, not modify files
        r = self._run(["baseline", "deprecate", "--orphaned"], shadow, baseline, root)
        assert r.returncode == 0
        assert "dry-run" in r.stdout.lower() or "Dry-run" in r.stdout
        assert "--apply" in r.stdout
        statuses = [_json.loads(f.read_text())["metadata"]["scenario_status"] for f in baseline.glob("*.json")]
        assert all(s == "approved" for s in statuses), "dry-run must not modify files"

    def test_orphaned_apply_deprecates_unmatched(self, tmp_path):
        """--orphaned --apply deprecates scenarios missing from shadow_data."""
        shadow = tmp_path / "shadow"
        baseline = tmp_path / "baseline"
        shadow.mkdir()
        baseline.mkdir()
        root = __import__("pathlib").Path(__file__).parent.parent

        # Record baseline (4 scenarios)
        r = self._run(["record", "pricing.py"], shadow, baseline, root)
        assert r.returncode == 0

        # Remove all captures — all 4 become orphaned
        import shutil

        shutil.rmtree(shadow)
        shadow.mkdir()
        # Add a dummy capture so shadow is not empty but matches nothing
        (shadow / "other.module.fn_abcdef1234567890abcdef1234567890_123456_1.json").write_text(
            '{"function":"fn","module":"other","semantic_id":"abcdef1234567890abcdef1234567890",'
            '"engine_version":"0.2.0","timestamp":"2025-01-01T00:00:00+00:00",'
            '"input":{},"output":{},"dependencies":[],"execution_metadata":{},'
            '"dependency_capture":{},"source":""}',
            encoding="utf-8",
        )

        r = self._run(["baseline", "deprecate", "--orphaned", "--apply"], shadow, baseline, root)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "deprecated" in r.stdout.lower()

        # All 4 original scenarios should now be deprecated
        import json

        statuses = [json.loads(f.read_text())["metadata"]["scenario_status"] for f in baseline.glob("*.json")]
        assert all(s == "deprecated" for s in statuses), statuses

    def test_orphaned_no_orphans_reports_clean(self, tmp_path):
        """--orphaned reports nothing to do when all baseline has matching captures."""
        shadow = tmp_path / "shadow"
        baseline = tmp_path / "baseline"
        shadow.mkdir()
        baseline.mkdir()
        root = __import__("pathlib").Path(__file__).parent.parent

        # Record — shadow and baseline match perfectly
        r = self._run(["record", "pricing.py"], shadow, baseline, root)
        assert r.returncode == 0

        r = self._run(["baseline", "deprecate", "--orphaned", "--apply"], shadow, baseline, root)
        assert r.returncode == 0
        assert "no orphaned" in r.stdout.lower()

    def test_single_deprecate_still_works(self, tmp_path):
        """Original single-scenario deprecate path is unaffected."""
        shadow = tmp_path / "shadow"
        baseline = tmp_path / "baseline"
        shadow.mkdir()
        baseline.mkdir()
        root = __import__("pathlib").Path(__file__).parent.parent

        r = self._run(["record", "pricing.py"], shadow, baseline, root)
        assert r.returncode == 0

        import json

        bf = sorted(baseline.glob("*.json"))[0]
        d = json.loads(bf.read_text())
        sid = f"{d['scenario']['module']}.{d['scenario']['function']}:{d['scenario']['semantic_id']}"

        r = self._run(["baseline", "deprecate", sid], shadow, baseline, root)
        assert r.returncode == 0
        assert "deprecated" in r.stdout.lower()
        updated = json.loads(bf.read_text())
        assert updated["metadata"]["scenario_status"] == "deprecated"

    def test_no_args_exits_with_error(self, tmp_path):
        """Running deprecate with no args and no --orphaned is an error."""
        shadow = tmp_path / "shadow"
        shadow.mkdir()
        baseline = tmp_path / "baseline"
        baseline.mkdir()
        root = __import__("pathlib").Path(__file__).parent.parent
        r = self._run(["baseline", "deprecate"], shadow, baseline, root)
        assert r.returncode == 2


class TestRecordHttpBaselineNotLost:
    """sst record must write HTTP-style baseline files to the top-level
    baseline directory, not into subdirectories created by path separators
    in the function name (e.g. 'POST /api/orders')."""

    def test_http_function_name_does_not_create_subdirectory(self, tmp_path):
        import json, glob as _glob, sys, subprocess, os
        from sst.governance import save_baseline_record, create_baseline_from_capture

        baseline = tmp_path / "baseline"
        baseline.mkdir()

        # Simulate what sst record does after reading an HTTP capture file
        sid = "a" * 32
        cap = {
            "module": "http",
            "function": "POST /api/orders",
            "semantic_id": sid,
            "output": {"raw_result": {"status": 200}},
            "engine_version": "0.2.0",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "input": {},
            "dependencies": [],
            "execution_metadata": {},
            "dependency_capture": {},
            "source": "",
        }

        # Apply the fix: sanitise function name before building baseline_name
        safe_func = cap["function"].replace(" ", "").replace("/", "_")
        baseline_name = f"{cap['module']}.{safe_func}_{cap['semantic_id']}.json"
        baseline_path = baseline / baseline_name

        record = create_baseline_from_capture(cap)
        save_baseline_record(str(baseline_path), record)

        # Must be a top-level file, not in a subdirectory
        top_level = _glob.glob(str(baseline / "*.json"))
        assert len(top_level) == 1, (
            f"Expected 1 top-level .json, got {len(top_level)}. "
            f"Contents: {list(baseline.rglob('*'))}"
        )
        assert "POST_api_orders" in top_level[0], (
            f"Unexpected filename: {top_level[0]}"
        )
        # Filename must match _BASELINE_FILENAME_RE so list_scenarios can read it
        import re

        BASELINE_RE = re.compile(r"^(.+)_([0-9a-f]{32})\.json$")
        fname = os.path.basename(top_level[0])
        assert BASELINE_RE.match(fname), f"Filename does not match regex: {fname!r}"
