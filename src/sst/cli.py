import glob
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
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

_CAPTURE_FILENAME_RE = re.compile(r"^(?P<mod_func>.+)_(?P<sid>[0-9a-f]{32})_\d{6}_\d+\.json$")
_MAX_OUTPUT_BYTES = 4096
MAX_CAPTURE_AGE_SECONDS = 60 * 60  # 1 hour
_PII_WARNING_EMITTED = False


def _pii_warning_enabled() -> bool:
    return os.getenv("SST_QUIET_PII_WARNING", "").strip().lower() not in {"1", "true", "yes", "on"}


def _emit_strict_pii_warning_if_needed() -> None:
    global _PII_WARNING_EMITTED
    cfg = refresh_config()
    if cfg.strict_pii_matching and _pii_warning_enabled() and not _PII_WARNING_EMITTED:
        click.echo(
            "NOTE: strict_pii_matching=true uses exact key matching only. "
            "Compound keys like 'user_password' and 'access_token' are not masked by key name. "
            "Set strict_pii_matching=false for substring matching (or set SST_QUIET_PII_WARNING=1 to silence this note)."
        )
        _PII_WARNING_EMITTED = True


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


@main.command(short_help="Record baseline scenarios (alias: rec)")
@click.argument("app_script")
@click.option("--clean", is_flag=True, default=False, help="Clean shadow_dir before recording to avoid mixing old captures")
def record(app_script, clean):
    """Record production baseline behavior."""
    if not os.path.exists(app_script):
        click.echo(f"Error: {app_script} not found.")
        return

    click.echo(f"Recording baseline from {app_script}...")
    _emit_strict_pii_warning_if_needed()
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

    files = sorted(glob.glob(os.path.join(config.shadow_dir, "*.json")))
    saved_count = 0
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                capture_data = json.load(handle)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping corrupted JSON file %s: %s", file_path, exc)
            continue

        try:
            safe_func = capture_data['function'].replace(' ', '').replace('/', '_')
            baseline_name = f"{capture_data['module']}.{safe_func}_{capture_data['semantic_id']}.json"
            baseline_record = create_baseline_from_capture(capture_data)
            save_baseline_record(os.path.join(config.baseline_dir, baseline_name), baseline_record)
            saved_count += 1
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping capture file %s: %s", file_path, exc)
            continue

    if process_failed and not files:
        click.echo("Error: Script failed and no captures were saved.")
        return

    if process_failed:
        click.echo(
            f"WARNING: PARTIAL BASELINE. Script failed during record; saved {saved_count} scenario(s). "
            "Review baseline contents before committing."
        )

    click.echo(f"Baseline recorded: {saved_count} scenarios saved to {config.baseline_dir}/")


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
    if report["capture_count"] < report["baseline_count"]:
        click.echo(
            f"WARNING: Replay captured {report['capture_count']}/{report['baseline_count']} baseline scenario(s)."
        )
        click.echo(
            "Coverage gap detected: ensure replay inputs are complete and middleware sampling is 1.0 during verify."
        )
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


def _truncate_output(text: str, limit: int = _MAX_OUTPUT_BYTES) -> str:
    """Truncate subprocess output to avoid bloating CI error messages."""
    if len(text) <= limit:
        return text
    kept = text[:limit]
    dropped = len(text) - limit
    return f"{kept}\n... [{dropped} chars truncated]"


def _collect_replay_capture(app_script: str, capture_dir: str) -> None:
    """Execute the target app in capture mode and persist replay artifacts."""
    env = os.environ.copy()
    env["SST_ENABLED"] = "true"
    env["SST_STORAGE_DIR"] = capture_dir
    env["SST_SAMPLING_RATE"] = "1.0"
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
            details.append(f"Stdout: {_truncate_output(stdout)}")
        if stderr:
            details.append(f"Stderr: {_truncate_output(stderr)}")
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
        shadow = Path(config.shadow_dir)
        shadow.mkdir(parents=True, exist_ok=True)
        for item in Path(capture_dir).glob("*.json"):
            match = _CAPTURE_FILENAME_RE.match(item.name)
            if match:
                mod_func = match.group("mod_func")
                semantic_id = match.group("sid")
                for stale in shadow.glob(f"{mod_func}_{semantic_id}_*.json"):
                    stale.unlink()
            shutil.copy2(item, shadow / item.name)
        engine = ReplayEngine(baseline_dir=config.baseline_dir, capture_dir=capture_dir)
        return engine.replay()


@main.command(short_help="Verify against baseline (alias: ver)")
@click.argument("app_script")
@click.option("--verbose", is_flag=True, help="Show field-level diff details")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable diff report")
def verify(app_script, verbose, json_output):
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


@main.command(name="rec", short_help="Short alias for record")
@click.argument("app_script")
@click.option("--clean", is_flag=True, default=False, help="Clean shadow_dir before recording to avoid mixing old captures")
@click.pass_context
def record_alias(ctx, app_script, clean):
    """Alias for `record`."""
    ctx.invoke(record, app_script=app_script, clean=clean)


@main.command(name="ver", short_help="Short alias for verify")
@click.argument("app_script")
@click.option("--verbose", is_flag=True, help="Show field-level diff details")
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable diff report")
@click.pass_context
def verify_alias(ctx, app_script, verbose, json_output):
    """Alias for `verify`."""
    ctx.invoke(verify, app_script=app_script, verbose=verbose, json_output=json_output)


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


def _find_recent_capture(shadow_dir: str, func_path: str, semantic_id: str) -> str | None:
    """Find newest capture and ensure it is not older than max allowed age."""
    pattern = os.path.join(shadow_dir, f"{func_path}_{semantic_id}_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return None
    newest = files[0]
    max_age = int(os.getenv("SST_MAX_CAPTURE_AGE_SECONDS", MAX_CAPTURE_AGE_SECONDS))
    if time.time() - os.path.getmtime(newest) > max_age:
        return None
    return newest


@main.command(short_help="Approve intentional behavior change (alias: ap)")
@click.argument("identifier")
@click.argument("semantic_id", required=False)
@click.option("--force", is_flag=True, help="Use capture even if it is older than 1 hour.")
def approve(identifier, semantic_id, force):
    """Approve an intentional change in behavior."""
    func_path, semantic_id = _parse_approval_target(identifier, semantic_id)
    func_path = func_path.replace(" ", "").replace("/", "_")
    config = refresh_config()
    capture_file = _find_recent_capture(config.shadow_dir, func_path, semantic_id)

    if capture_file is None:
        pattern = os.path.join(config.shadow_dir, f"{func_path}_{semantic_id}_*.json")
        old_files = sorted(glob.glob(pattern), reverse=True)

        if old_files and not force:
            age_min = int((time.time() - os.path.getmtime(old_files[0])) / 60)
            click.echo(
                f"Error: Most recent capture for '{func_path}:{semantic_id}' "
                f"is {age_min} minutes old.\n"
                "Run 'sst verify <app>' first, then approve.\n"
                "Or use --force to approve from the existing capture."
            )
            sys.exit(2)
        if old_files and force:
            capture_file = old_files[0]
            age_min = int((time.time() - os.path.getmtime(capture_file)) / 60)
            click.echo(f"Warning: Using capture that is {age_min} minutes old (--force).")
        else:
            click.echo(
                f"Error: No capture found for '{func_path}:{semantic_id}'.\n"
                "Run 'sst verify <app>' first.\n"
                "Hint: module is '__main__' when running scripts directly."
            )
            sys.exit(2)

    with open(capture_file, "r", encoding="utf-8") as handle:
        capture_data = json.load(handle)

    os.makedirs(config.baseline_dir, exist_ok=True)
    baseline_path = os.path.join(config.baseline_dir, f"{func_path}_{semantic_id}.json")
    approve_scenario(baseline_path, capture_data)
    click.echo(f"Approved: {func_path}:{semantic_id}. Baseline updated.")


@main.command(name="ap", short_help="Short alias for approve")
@click.argument("identifier")
@click.argument("semantic_id", required=False)
@click.option("--force", is_flag=True, help="Use capture even if it is older than 1 hour.")
@click.pass_context
def approve_alias(ctx, identifier, semantic_id, force):
    """Alias for `approve`."""
    ctx.invoke(approve, identifier=identifier, semantic_id=semantic_id, force=force)


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
            if row.get("_warning"):
                click.echo(f"WARN: {row['_warning']}", err=True)
                continue
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


def _find_orphaned_scenarios(baseline_dir: str, shadow_dir: str) -> list[dict]:
    """Return baseline scenarios that have no matching capture in shadow_dir.

    A scenario is considered orphaned when its ``module.function:semantic_id``
    key does not appear in any capture file currently in *shadow_dir*.  The
    comparison is based on the capture filename pattern
    ``<module>.<function>_<semantic_id>_<timestamp>.json`` already used
    elsewhere in the CLI.

    Returns a list of dicts with keys ``scenario_id`` and ``file``.

    Raises ``SSTError`` when *shadow_dir* does not exist or is empty — the
    caller cannot determine orphan status without capture data.
    """
    if not os.path.exists(shadow_dir) or not glob.glob(os.path.join(shadow_dir, "*.json")):
        raise SSTError(
            "NO_CAPTURES",
            "USER",
            f"shadow_dir '{shadow_dir}' is empty or does not exist. "
            "Run 'sst record <script>' first to populate it.",
        )

    # Build set of "module.function:semantic_id" keys present in captures.
    # SSTMiddleware replaces "/" with "_" in the path when writing the capture
    # filename (e.g. "POST /api/orders" -> "POST_api_orders" in the filename).
    # Normalise the same way so HTTP captures match their baselines.
    captured_keys: set[str] = set()
    for cap_path in glob.glob(os.path.join(shadow_dir, "*.json")):
        m = _CAPTURE_FILENAME_RE.match(os.path.basename(cap_path))
        if m:
            captured_keys.add(f"{m.group('mod_func')}:{m.group('sid')}")

    orphaned = []
    for row in list_scenarios(baseline_dir):
        if row.get("_warning"):
            click.echo(f"WARN: {row['_warning']}", err=True)
            continue
        sid = row["scenario_id"]
        if not sid:
            # _filename_to_scenario_id returns None for files whose names do not
            # match _BASELINE_FILENAME_RE. Skip to avoid a downstream AttributeError.
            continue
        if sid not in captured_keys:
            orphaned.append({"scenario_id": sid, "file": row["file"]})
    return orphaned


@baseline.command("deprecate")
@click.argument("scenario_id", required=False, default=None)
@click.option(
    "--orphaned",
    is_flag=True,
    default=False,
    help="Deprecate all baseline scenarios that have no matching capture in shadow_data.",
)
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Actually apply deprecations (default is dry-run, which only lists candidates).",
)
def baseline_deprecate(scenario_id, orphaned, apply):
    """Mark a baseline scenario as deprecated.

    With --orphaned, finds all scenarios that have no matching capture in
    shadow_data and deprecates them. Runs as dry-run by default; pass --apply
    to commit the changes.
    """
    config = refresh_config()

    if orphaned:
        try:
            candidates = _find_orphaned_scenarios(config.baseline_dir, config.shadow_dir)
        except SSTError as exc:
            click.echo(f"Error: {exc.explanation}")
            sys.exit(2)

        if not candidates:
            click.echo("No orphaned scenarios found.")
            return

        if not apply:
            click.echo(f"Dry-run: {len(candidates)} orphaned scenario(s) would be deprecated:")
            for c in candidates:
                click.echo(f"  {c['scenario_id']}")
            click.echo("Re-run with --apply to commit.")
            return

        deprecated_count = 0
        for c in candidates:
            try:
                path = find_scenario_file(config.baseline_dir, c["scenario_id"])
                deprecate_scenario(path)
                click.echo(f"Deprecated {c['scenario_id']}")
                deprecated_count += 1
            except SSTError as exc:
                click.echo(f"Warning: could not deprecate {c['scenario_id']}: {exc.explanation}")

        click.echo(f"Done: {deprecated_count} scenario(s) deprecated.")
        return

    # Original single-scenario path
    if not scenario_id:
        click.echo("Error: provide a SCENARIO_ID or use --orphaned.")
        sys.exit(2)

    try:
        path = find_scenario_file(config.baseline_dir, scenario_id)
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
