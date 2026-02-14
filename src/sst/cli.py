import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import click

from . import __version__ as VERSION
from .config import refresh_config
from .errors import SSTError, ScenarioNotFoundError
from .governance import (
    approve_scenario,
    create_baseline_from_capture,
    deprecate_scenario,
    find_scenario_file,
    list_scenarios,
    load_baseline_record,
    save_baseline_record,
)
from .replay import ReplayEngine
from .types import ReplayReport
from .synthesizer import SSTSynthesizer

logger = logging.getLogger(__name__)



@click.group(invoke_without_command=True)
@click.pass_context
@click.option("--version", is_flag=True, help="Show the version and exit.")
def main(ctx, version):
    """SST: Semantic Shadow Testing CLI"""
    ctx.obj = {"config": refresh_config()}

    if version:
        click.echo(f"SST version {VERSION}")
        return
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.option("--func", help="Specific function name to generate")
@click.option("--all", "generate_all", is_flag=True, help="Generate tests for all captured functions")
@click.option("--output-dir", default="tests/", help="Output directory")
@click.option("--model", help="Override the LLM model (sets SST_MODEL env var)")
@click.option("--provider", help="LLM provider: openai, anthropic, ollama, lmstudio, local (sets SST_PROVIDER env var)")
@click.option("--edit", is_flag=True, help="Open generated tests in editor for quick-fix")
def generate(func, generate_all, output_dir, model, provider, edit):
    """Generate Pytest files from captured data.

    Providers: openai, anthropic, ollama, lmstudio, local.
    """
    config = refresh_config()
    if not os.path.exists(config.shadow_dir) or not any(fname.endswith(".json") for fname in os.listdir(config.shadow_dir)):
        click.echo(f"Error: No captured data found in {config.shadow_dir}. Run your app with SST_ENABLED=true first.")
        return

    if model:
        os.environ["SST_MODEL"] = model
    if provider:
        os.environ["SST_PROVIDER"] = provider

    if not func and not generate_all:
        click.echo("Please specify --func <name> or use --all to generate all tests.")
        return

    click.echo(f"Generating tests in {output_dir}...")
    SSTSynthesizer().run(func_filter=func, output_dir=output_dir, open_editor=edit)
    click.echo("Done.")


@main.command(name="help")
@click.pass_context
def help_command(ctx):
    """Show this message and exit."""
    click.echo(ctx.parent.get_help())


@main.command()
def clean():
    """Remove all captured shadow data."""
    config = refresh_config()
    if os.path.exists(config.shadow_dir):
        shutil.rmtree(config.shadow_dir)
        click.echo(f"Cleaned {config.shadow_dir}")
    else:
        click.echo("Nothing to clean.")


@main.command()
@click.argument("app_script")
@click.option("--clean", is_flag=True, default=False, help="Clean shadow_dir before recording to avoid mixing old captures")
def record(app_script, clean):
    """Record production baseline behavior."""
    if not os.path.exists(app_script):
        click.echo(f"Error: {app_script} not found.")
        return

    click.echo(f"Recording baseline from {app_script}...")
    config = refresh_config()
    os.makedirs(config.shadow_dir, exist_ok=True)
    if config.clean_shadow_on_record or clean:
        shutil.rmtree(config.shadow_dir, ignore_errors=True)
        os.makedirs(config.shadow_dir, exist_ok=True)
    elif os.listdir(config.shadow_dir):
        click.echo("Warning: shadow_dir not empty — may mix old captures")

    env = os.environ.copy()
    env["SST_ENABLED"] = "true"

    process_failed = False
    try:
        subprocess.run([sys.executable, app_script], check=True, env=env)
    except subprocess.CalledProcessError as exc:
        click.echo(f"Warning: Script exited with code {exc.returncode}. Attempting to save partial baseline...")
        process_failed = True

    os.makedirs(config.baseline_dir, exist_ok=True)

    files = glob.glob(os.path.join(config.shadow_dir, "*.json"))
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                capture_data = json.load(handle)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping corrupted JSON file %s: %s", file_path, exc)
            continue

        baseline_name = f"{capture_data['module']}.{capture_data['function']}_{capture_data['semantic_id']}.json"
        baseline_record = create_baseline_from_capture(capture_data)
        save_baseline_record(os.path.join(config.baseline_dir, baseline_name), baseline_record)

    if process_failed and not files:
        click.echo("Error: Script failed and no captures were saved.")
        return

    click.echo(f"Baseline recorded: {len(files)} scenarios saved to {config.baseline_dir}/")


def _verify_timestamp() -> str:
    """Return deterministic UTC timestamp string used in verify reports."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _build_ci_json_report(report: ReplayReport):
    scenario_rows = [
        {
            "scenario_id": row["scenario_id"],
            "status": "pass" if row["status"] == "passed" else "fail",
            "diff_summary": "" if row["status"] == "passed" else row.get("summary", ""),
            "diff": row["changes"],
            "baseline_version": row.get("baseline_version"),
        }
        for row in sorted(report.get("scenarios", []), key=lambda item: item["scenario_id"])
    ]
    mismatch_count = sum(1 for row in scenario_rows if row["status"] == "fail")
    return {
        "summary": {
            "timestamp": _verify_timestamp(),
            "sst_version": VERSION,
            "baseline_count": report["baseline_count"],
            "capture_count": report["capture_count"],
            "mismatch_count": mismatch_count,
            "warning_count": len(report.get("warnings", [])),
        },
        "warnings": report.get("warnings", []),
        "scenarios": scenario_rows,
        "exit_code": 1 if mismatch_count else 0,
    }


def _emit_structured_error(message: str, *, code: str, category: str, as_json: bool = False, exit_code: int = 2):
    payload = {
        "ok": False,
        "error": {
            "code": code,
            "category": category,
            "message": message,
        },
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        prefix = "SST internal error" if code == "INTERNAL" else "SST error"
        click.echo(f"{prefix} [{category}:{code}]: {message}")
    sys.exit(exit_code)


def _print_verify_report(report: ReplayReport, verbose: bool = False, as_json: bool = False):
    if as_json:
        click.echo(json.dumps(_build_ci_json_report(report), indent=2, sort_keys=True))
        return

    click.echo("SST Verification Report")
    click.echo("-----------------------")
    click.echo(f"Scenarios checked: {report['baseline_count']}")
    click.echo(f"Regressions: {len(report['regressions']) + len(report['missing'])}")
    click.echo()

    for warning in report.get("warnings", []):
        click.echo(f"WARN: {warning}")
    if report.get("warnings"):
        click.echo()

    for scenario in sorted(report.get("scenarios", []), key=lambda item: item["scenario_id"]):
        if scenario["status"] == "passed":
            click.echo(f"PASS: {scenario['scenario_id']}")
            continue

        click.echo(f"FAIL: {scenario['scenario_id']}")
        click.echo(f"Baseline version: {scenario.get('baseline_version') or 'unknown'}")
        click.echo(f"Summary: {scenario.get('summary', '')}")

        if scenario["human_diff"]:
            click.echo(scenario["human_diff"])
        elif scenario["changes"]:
            first = scenario["changes"][0]
            click.echo(f"Field changed: {first['path']}")
            click.echo(f"Expected: {first.get('baseline')}")
            click.echo(f"Actual: {first.get('current')}")

        if verbose and scenario["changes"] and not scenario["human_diff"]:
            click.echo(json.dumps(scenario["changes"], indent=2, sort_keys=True))

        click.echo(f"To approve intentional changes:\n  sst approve {scenario['scenario_id']}")
        click.echo()


def _collect_replay_capture(app_script: str, capture_dir: str) -> None:
    """Execute the target app in capture mode and persist replay artifacts."""
    env = os.environ.copy()
    env["SST_ENABLED"] = "true"
    env["SST_STORAGE_DIR"] = capture_dir
    env.setdefault("PYTHONHASHSEED", "0")
    env.setdefault("SST_REPLAY_SEED", "0")

    timeout = refresh_config().verify_timeout

    try:
        result = subprocess.run([sys.executable, app_script], capture_output=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SSTError(
            "VERIFY_TIMEOUT",
            "SYSTEM",
            f"Verify script {app_script} exceeded timeout of {timeout}s. Increase SST_VERIFY_TIMEOUT if needed."
        ) from exc
    if result.returncode != 0:
        raw_stdout = getattr(result, "stdout", b"") or b""
        raw_stderr = getattr(result, "stderr", b"") or b""
        if isinstance(raw_stdout, str):
            stdout = raw_stdout.strip()
        else:
            stdout = raw_stdout.decode("utf-8", errors="replace").strip()
        if isinstance(raw_stderr, str):
            stderr = raw_stderr.strip()
        else:
            stderr = raw_stderr.decode("utf-8", errors="replace").strip()
        details = []
        if stdout:
            details.append(f"Stdout: {stdout}")
        if stderr:
            details.append(f"Stderr: {stderr}")
        detail = "\n" + "\n".join(details) if details else ""
        raise SSTError(
            "VERIFY_REPLAY_CAPTURE_FAILED",
            "SYSTEM",
            f"Replay capture failed while executing {app_script}.{detail}",
        )


def _run_verify_pipeline(app_script: str) -> ReplayReport:
    """Run SST verify pipeline: baseline load -> replay -> diff -> scenario report."""
    with tempfile.TemporaryDirectory(prefix="sst_verify_") as capture_dir:
        _collect_replay_capture(app_script, capture_dir)
        config = refresh_config()
        engine = ReplayEngine(baseline_dir=config.baseline_dir, capture_dir=capture_dir)
        return engine.replay()


@main.command()
@click.argument("app_script")
@click.option("--verbose", is_flag=True, help="Show field-level diff details")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable diff report")
@click.option("--replay", is_flag=True, help="Deterministic replay using captured inputs")
def verify(app_script, verbose, json_output, replay):
    """Verify current behavior against baseline (Regression Gate)."""
    if not os.path.exists(app_script):
        click.echo(f"Error: {app_script} not found.")
        sys.exit(2)
    config = refresh_config()
    if not os.path.exists(config.baseline_dir) or not os.listdir(config.baseline_dir):
        click.echo("Error: No baseline found. Run 'sst record <app_script>' first.")
        sys.exit(2)

    try:
        if not json_output:
            click.echo(f"Verifying {app_script} against baseline...")
        report = _run_verify_pipeline(app_script)
        exit_code = 1 if (report["regressions"] or report["missing"]) else 0
        _print_verify_report(report, verbose=verbose, as_json=json_output)
        sys.exit(exit_code)
    except SystemExit:
        raise
    except SSTError as exc:
        _emit_structured_error(exc.explanation, code=exc.error_code, category=exc.category, as_json=json_output, exit_code=2)
    except Exception as exc:  # defensive guard for CI stability
        logger.exception("Unhandled SST verify error")
        _emit_structured_error(str(exc), code="INTERNAL", category="SYSTEM", as_json=json_output, exit_code=2)


def _parse_approval_target(identifier: str, semantic_id: str | None) -> tuple[str, str]:
    """Support both legacy and scenario-id approve command formats."""
    if semantic_id:
        return identifier, semantic_id
    if ":" not in identifier:
        raise click.UsageError("Expected <module.function:semantic_id> or <module.function> <semantic_id>.")
    func_path, scenario_id = identifier.split(":", 1)
    if not func_path or not scenario_id:
        raise click.UsageError("Expected <module.function:semantic_id> format.")
    return func_path, scenario_id


@main.command()
@click.argument("identifier")
@click.argument("semantic_id", required=False)
def approve(identifier, semantic_id):
    """Approve an intentional change in behavior."""
    func_path, semantic_id = _parse_approval_target(identifier, semantic_id)
    config = refresh_config()
    pattern = os.path.join(config.shadow_dir, f"{func_path}_{semantic_id}_*.json")
    files = sorted(glob.glob(pattern), reverse=True)

    if not files:
        click.echo(f"Error: No recent capture found for {func_path} with ID {semantic_id}")
        return

    with open(files[0], "r", encoding="utf-8") as handle:
        capture_data = json.load(handle)

    os.makedirs(config.baseline_dir, exist_ok=True)
    baseline_path = os.path.join(config.baseline_dir, f"{func_path}_{semantic_id}.json")
    approve_scenario(baseline_path, capture_data)
    click.echo(f"Approved change for {func_path} ({semantic_id}). Baseline updated.")


@main.group()
def baseline():
    """Baseline governance commands."""


@baseline.command("list")
def baseline_list():
    """List baseline scenarios and governance metadata."""
    config = refresh_config()
    if not os.path.exists(config.baseline_dir):
        click.echo("No baseline directory found.")
        return
    try:
        for row in list_scenarios(config.baseline_dir):
            meta = row["metadata"]
            click.echo(f"{row['scenario_id']} status={meta['scenario_status']} version={meta['version_id']}")
    except SSTError as exc:
        click.echo(f"Error: {exc.explanation}")
        sys.exit(2)


@baseline.command("show")
@click.argument("scenario_id")
def baseline_show(scenario_id):
    """Show baseline scenario details."""
    try:
        path = find_scenario_file(refresh_config().baseline_dir, scenario_id)
    except SSTError as exc:
        click.echo(f"Error: {exc}")
        sys.exit(2)
    try:
        record = load_baseline_record(path)
    except SSTError as exc:
        click.echo(f"Error: {exc.explanation}")
        sys.exit(2)
    click.echo(json.dumps(record, indent=2, sort_keys=True))


@baseline.command("deprecate")
@click.argument("scenario_id")
def baseline_deprecate(scenario_id):
    """Mark a baseline scenario as deprecated."""
    try:
        path = find_scenario_file(refresh_config().baseline_dir, scenario_id)
    except SSTError as exc:
        click.echo(f"Error: {exc}")
        sys.exit(2)
    try:
        record = deprecate_scenario(path)
    except SSTError as exc:
        click.echo(f"Error: {exc.explanation}")
        sys.exit(2)
    click.echo(f"Deprecated {scenario_id} (version={record['metadata']['version_id']})")


if __name__ == "__main__":
    main()
