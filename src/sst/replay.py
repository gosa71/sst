"""Deterministic replay support for SST verify.

Replay guarantees:
- baseline and replay inputs are validated at load boundaries;
- non-deterministic replay inputs (duplicate scenario keys) fail explicitly;
- corrupted or incompatible baseline data fails loudly.
"""

from __future__ import annotations

import glob
import os
from typing import Any, Dict, List

from . import __version__
from .config import get_config
from .diff import apply_diff_policy, build_structured_diff, diff_policy_snapshot, format_human_diff, normalize_for_compare, summarize_changes
from .errors import BaselineValidationError, ReplayDeterminismError, ReplayExecutionError
from .governance import governance_policy_snapshot, load_baseline_record
from .types import BaselineRecord, CaptureScenario, ReplayReport, ReplayScenarioResult, validate_capture_scenario


def _load_capture_file(path: str) -> dict:
    """Read and validate a raw capture payload file."""
    from .governance import load_baseline_record as _load_baseline
    import json

    max_size = get_config().max_baseline_size
    if os.path.getsize(path) > max_size:
        raise ReplayExecutionError(
            f"Capture file exceeds maximum size ({max_size} bytes): {path}"
        )

    with open(path, "r", encoding="utf-8") as file_obj:
        try:
            data = json.load(file_obj)
        except json.JSONDecodeError as exc:
            raise ReplayExecutionError(
                f"Invalid JSON in capture file '{path}': {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise ReplayExecutionError(
            f"Capture file '{path}' must contain a JSON object at top level"
        )

    if "scenario" not in data:
        data = {"scenario": data, "metadata": {}, "approval_history": []}

    return data


class ReplayEngine:
    """Replay scenarios by matching captured input signatures against baseline outputs."""

    def __init__(self, baseline_dir: str, capture_dir: str):
        self.baseline_dir = baseline_dir
        self.capture_dir = capture_dir

    def _scenario_key(self, scenario: CaptureScenario) -> str:
        validated = validate_capture_scenario(scenario)
        return f"{validated['module']}.{validated['function']}:{validated['semantic_id']}"

    def _load_baselines(self) -> Dict[str, BaselineRecord]:
        records: Dict[str, BaselineRecord] = {}
        record_files: Dict[str, str] = {}
        for path in sorted(glob.glob(os.path.join(self.baseline_dir, "*.json"))):
            record = load_baseline_record(path)
            scenario = record["scenario"]
            if record["metadata"].get("scenario_status") == "deprecated":
                continue
            key = self._scenario_key(scenario)
            if key in records:
                existing_file = record_files[key]
                raise ReplayExecutionError(
                    f"Duplicate scenario key detected: {key}. "
                    f"Files: {existing_file} and {os.path.basename(path)}"
                )
            records[key] = record
            record_files[key] = os.path.basename(path)
        return records

    def _load_captures(self) -> Dict[str, BaselineRecord]:
        records: Dict[str, BaselineRecord] = {}
        for path in sorted(glob.glob(os.path.join(self.capture_dir, "*.json"))):
            record = _load_capture_file(path)
            scenario = record["scenario"]
            key = self._scenario_key(scenario)
            if key in records:
                raise ReplayDeterminismError(
                    f"Replay capture is non-deterministic: duplicate scenario key '{key}' in capture artifacts.\n\n"
                    f"Hint: the shadow directory contains captures from multiple runs.\n"
                    f"Clear the shadow directory before verify:\n"
                    f"  rm -rf <shadow_dir>/ && python <app.py> && sst verify <app.py>\n"
                    f"Conflicting file: {path}"
                )
            records[key] = record
        return records

    def normalize_output(self, value: Any) -> Any:
        """Internal replay normalization hook for deterministic comparisons."""
        return normalize_for_compare(apply_diff_policy(value))


    def _engine_version_warning(self, baseline_scenario: CaptureScenario) -> str | None:
        baseline_engine_version = baseline_scenario.get("engine_version")
        if not baseline_engine_version:
            return (
                "Baseline missing engine_version metadata — potential reinterpretation risk "
                f"(current SST v{__version__})."
            )
        if baseline_engine_version != __version__:
            return (
                f"Baseline captured with SST v{baseline_engine_version}, current v{__version__} "
                "— potential reinterpretation risk"
            )
        return None

    def _policy_drift_messages(self, baseline_record: BaselineRecord) -> list[str]:
        messages: list[str] = []
        metadata = baseline_record.get("metadata", {})

        baseline_diff_snapshot = metadata.get("diff_policy_snapshot")
        current_diff_snapshot = diff_policy_snapshot()
        if not baseline_diff_snapshot:
            messages.append("Baseline missing diff_policy_snapshot metadata — policy drift cannot be proven.")
        else:
            baseline_semantics = int(baseline_diff_snapshot.get("semantics_version", 0) or 0)
            current_semantics = int(current_diff_snapshot.get("semantics_version", 0) or 0)
            if baseline_semantics < current_semantics:
                messages.append(
                    "POLICY_DRIFT: DiffPolicy semantics_version advanced "
                    f"from {baseline_semantics} to {current_semantics}; normalization semantics may differ."
                )
            elif baseline_diff_snapshot.get("hash") != current_diff_snapshot.get("hash"):
                messages.append(
                    "POLICY_DRIFT: DiffPolicy snapshot mismatch between baseline and current runtime policy."
                )

        baseline_governance_snapshot = metadata.get("governance_policy_snapshot")
        current_governance = governance_policy_snapshot()
        if not baseline_governance_snapshot:
            messages.append("Baseline missing governance_policy_snapshot metadata — governance drift cannot be proven.")
        elif baseline_governance_snapshot.get("hash") != current_governance.get("hash"):
            messages.append("POLICY_DRIFT: GovernancePolicy snapshot mismatch between baseline and runtime.")

        return messages

    def replay(self) -> ReplayReport:
        try:
            baselines = self._load_baselines()
            captures = self._load_captures()
        except (BaselineValidationError, ReplayDeterminismError, ReplayExecutionError):
            raise
        except Exception as exc:
            raise ReplayExecutionError(f"Replay execution failed: {exc}") from exc

        regressions: List[ReplayScenarioResult] = []
        missing: List[str] = []
        scenarios: List[ReplayScenarioResult] = []
        warnings: List[str] = []

        for key, baseline_record in sorted(baselines.items()):
            baseline_scenario = baseline_record["scenario"]
            baseline_version = baseline_record["metadata"].get("version_id")

            scenario_warnings: List[str] = []
            engine_warning = self._engine_version_warning(baseline_scenario)
            if engine_warning:
                scenario_warnings.append(engine_warning)
                warnings.append(f"{key}: {engine_warning}")

            drift_messages = self._policy_drift_messages(baseline_record)
            for msg in drift_messages:
                warnings.append(f"{key}: {msg}")

            if any(msg.startswith("POLICY_DRIFT") for msg in drift_messages):
                row: ReplayScenarioResult = {
                    "scenario_id": key,
                    "status": "failed",
                    "summary": "; ".join(drift_messages),
                    "human_diff": "\n".join(drift_messages),
                    "changes": [
                        {
                            "path": "$.metadata",
                            "change_type": "POLICY_DRIFT",
                            "severity": "high",
                            "baseline": baseline_record.get("metadata", {}),
                            "current": {
                                "diff_policy_snapshot": diff_policy_snapshot(),
                                "governance_policy_snapshot": governance_policy_snapshot(),
                            },
                        }
                    ],
                    "baseline_version": baseline_version,
                    "warnings": scenario_warnings,
                }
                regressions.append(row)
                scenarios.append(row)
                continue

            if key not in captures:
                missing.append(key)
                scenarios.append(
                    {
                        "scenario_id": key,
                        "status": "failed",
                        "summary": "Scenario missing from replay capture.",
                        "human_diff": "No replay output captured for baseline scenario.",
                        "changes": [],
                        "baseline_version": baseline_version,
                        "warnings": scenario_warnings,
                    }
                )
                continue

            current_scenario = captures[key]["scenario"]
            baseline_output = self.normalize_output(baseline_scenario.get("output", {}).get("raw_result"))
            current_output = self.normalize_output(current_scenario.get("output", {}).get("raw_result"))
            changes = build_structured_diff(baseline_output, current_output)
            if changes:
                row: ReplayScenarioResult = {
                    "scenario_id": key,
                    "status": "failed",
                    "summary": summarize_changes(changes),
                    "human_diff": format_human_diff(changes),
                    "changes": changes,
                    "baseline_version": baseline_version,
                    "warnings": scenario_warnings,
                }
                regressions.append(row)
                scenarios.append(row)
            else:
                scenarios.append(
                    {
                        "scenario_id": key,
                        "status": "passed",
                        "summary": "No semantic differences detected.",
                        "human_diff": "",
                        "changes": [],
                        "baseline_version": baseline_version,
                        "warnings": scenario_warnings,
                    }
                )

        return {
            "regressions": regressions,
            "missing": missing,
            "scenarios": scenarios,
            "baseline_count": len(baselines),
            "capture_count": len(captures),
            "warnings": warnings,
        }
