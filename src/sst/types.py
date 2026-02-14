"""Typed data contracts used across SST capture, baseline, replay, and governance layers.

All public contracts expose runtime validators so module boundaries fail loudly when
payloads are malformed or semantically incompatible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, TypedDict

from .errors import BaselineValidationError
from .schema import validate_scenario_schema


class CaptureOutput(TypedDict, total=False):
    """Function execution output snapshot captured by SST.

    Invariant:
    - ``status`` is always present after capture finalization.
    - successful captures must include ``raw_result``.
    - failed captures must include ``error`` and ``error_type``.
    """

    raw_result: Any
    status: Literal["unknown", "success", "failure"]
    error: str
    error_type: str


class CaptureScenario(TypedDict, total=False):
    """Structured capture payload for one function scenario."""

    function: str
    module: str
    semantic_id: str
    engine_version: str
    timestamp: str
    input: Dict[str, Any]
    output: CaptureOutput
    dependencies: List[str]
    execution_metadata: Dict[str, str]
    dependency_capture: Dict[str, Any]
    source: str


class BaselineMetadata(TypedDict, total=False):
    """Governance metadata attached to each baseline scenario."""

    format_version: int
    version_id: str
    created_at: str
    approved_at: Optional[str]
    scenario_status: Literal["pending", "approved", "deprecated"]
    diff_policy_snapshot: Dict[str, Any]
    governance_policy_snapshot: Dict[str, Any]


class ApprovalHistoryEntry(TypedDict):
    """Single governance action recorded in baseline history."""

    approved_at: str
    action: Literal["record", "approve", "deprecate"]


class BaselineRecord(TypedDict):
    """Versioned persisted baseline contract.

    Invariant:
    - ``metadata.format_version`` is required and validated against loader schema.
    - ``scenario`` must include ``module``, ``function``, and ``semantic_id``.
    """

    scenario: CaptureScenario
    metadata: BaselineMetadata
    approval_history: List[ApprovalHistoryEntry]


class DiffChange(TypedDict, total=False):
    """Single structured diff change item."""

    path: str
    change_type: str
    severity: Literal["low", "medium", "high"]
    baseline: Any
    current: Any
    baseline_type: str
    current_type: str


class ReplayScenarioResult(TypedDict):
    """Per-scenario replay result used by reporting boundaries."""

    scenario_id: str
    status: Literal["passed", "failed"]
    summary: str
    human_diff: str
    changes: List[DiffChange]
    baseline_version: Optional[str]
    warnings: List[str]


class ReplayReport(TypedDict):
    """Replay summary payload consumed by verify CLI and CI output."""

    regressions: List[ReplayScenarioResult]
    missing: List[str]
    scenarios: List[ReplayScenarioResult]
    baseline_count: int
    capture_count: int
    warnings: List[str]


@dataclass(frozen=True)
class GovernanceDecision:
    """Auditable decision object for governance workflows.

    Guarantees:
    - deterministic fields for a given policy and inputs.
    - includes policy and decision identifiers for traceability.
    """

    allowed: bool
    reason_code: str
    human_explanation: str
    policy_id: str
    decision_id: str

    @property
    def explanation(self) -> str:
        """Backward-compatible alias for older call sites."""
        return self.human_explanation


@dataclass(frozen=True)
class CapturePayload:
    """Immutable normalized capture payload used by core orchestration."""

    function: str
    module: str
    semantic_id: str
    engine_version: str
    timestamp: str
    input: Dict[str, Any]
    output: CaptureOutput
    dependencies: List[str]
    execution_metadata: Dict[str, str]
    dependency_capture: Dict[str, Any]
    source: str


def validate_capture_scenario(scenario: Dict[str, Any]) -> CaptureScenario:
    """Validate a capture scenario payload at runtime.

    Raises:
        ValueError: if required keys are missing or have invalid types.
    """

    try:
        validate_scenario_schema(
            {
                "module": scenario.get("module"),
                "function": scenario.get("function"),
                "semantic_id": scenario.get("semantic_id"),
                "input": scenario.get("input", {}),
                "output": scenario.get("output", {}),
            }
        )
    except BaselineValidationError as exc:
        raise ValueError(exc.explanation) from exc
    return scenario  # type: ignore[return-value]


def validate_baseline_record(record: Dict[str, Any]) -> BaselineRecord:
    """Validate baseline record shape at module boundaries.

    Raises:
        ValueError: if shape invariants are violated.
    """

    if not isinstance(record, dict):
        raise ValueError("Baseline record must be a JSON object")
    if "scenario" not in record or not isinstance(record["scenario"], dict):
        raise ValueError("Baseline record missing required object field: scenario")
    # Full identity validation is enforced at execution boundaries (e.g., replay).
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("Baseline record missing required object field: metadata")
    if not isinstance(metadata.get("format_version"), int):
        raise ValueError("Baseline metadata missing required integer field: format_version")
    return record  # type: ignore[return-value]
