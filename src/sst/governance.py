"""Baseline governance helpers for SST baseline metadata and lifecycle.

Governance is policy-driven data. Policies are serializable and deterministic.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from . import __version__
from .config import get_config
from .diff import diff_policy_snapshot
from .errors import BaselineFormatError, GovernancePolicyError, ScenarioNotFoundError
from .sst_schema import validate_scenario_schema
from .types import BaselineRecord, GovernanceDecision, validate_baseline_record

BASELINE_FORMAT_VERSION = 1
SUPPORTED_BASELINE_VERSIONS = {1}
STRICT_GOVERNANCE = True
_CUSTOM_TRANSITION_VALIDATOR = None


@dataclass(frozen=True)
class GovernancePolicy:
    """Serializable governance policy with deterministic transitions."""

    policy_id: str
    transitions: Dict[Tuple[str, str], Tuple[bool, str, str]]


DEFAULT_GOVERNANCE_POLICY = GovernancePolicy(
    policy_id="default-governance-v1",
    transitions={
        ("approve", "pending"): (True, "APPROVE_ALLOWED", "Pending scenario approved and versioned."),
        ("approve", "approved"): (True, "REAPPROVE_ALLOWED", "Approved scenario updated with a new version."),
        ("approve", "deprecated"): (True, "REACTIVATE_ALLOWED", "Deprecated scenario reactivated and approved."),
        ("deprecate", "approved"): (True, "DEPRECATE_ALLOWED", "Approved scenario marked as deprecated."),
        ("deprecate", "pending"): (True, "DEPRECATE_PENDING_ALLOWED", "Pending scenario marked as deprecated."),
        ("deprecate", "deprecated"): (True, "NOOP_DEPRECATE", "Scenario already deprecated; action logged for audit."),
    },
)


def governance_policy_snapshot(policy: GovernancePolicy | None = None) -> Dict[str, Any]:
    """Serializable governance policy snapshot for baseline metadata."""
    effective = policy or resolve_governance_policy()
    transitions = [
        {
            "action": action,
            "from": current,
            "allowed": details[0],
            "reason_code": details[1],
            "explanation": details[2],
        }
        for (action, current), details in sorted(effective.transitions.items())
    ]
    payload = {"policy_id": effective.policy_id, "transitions": transitions}
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload["hash"] = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return payload


def resolve_governance_policy(policy_name: str | None = None) -> GovernancePolicy:
    selected = (policy_name or get_config().governance_policy or "default").lower()
    if selected in {"default", "default-v1"}:
        return DEFAULT_GOVERNANCE_POLICY
    raise GovernancePolicyError(f"Unsupported governance policy: {selected}")


def set_custom_transition_validator(custom_transition_validator):
    """Set optional governance transition validator hook.

    Expected signature:
        custom_transition_validator(old_state, new_state) -> bool | None
    """

    global _CUSTOM_TRANSITION_VALIDATOR
    _CUSTOM_TRANSITION_VALIDATOR = custom_transition_validator


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_metadata(*, include_policy_snapshots: bool = True) -> Dict[str, Any]:
    metadata = {
        "format_version": BASELINE_FORMAT_VERSION,
        "version_id": str(uuid.uuid4()),
        "created_at": utcnow_iso(),
        "approved_at": None,
        "scenario_status": "pending",
    }
    if include_policy_snapshots:
        metadata["diff_policy_snapshot"] = diff_policy_snapshot()
        metadata["governance_policy_snapshot"] = governance_policy_snapshot()
    return metadata


def _migrate_record_for_version(data: Dict[str, Any], version: int) -> Dict[str, Any]:
    """Migration hook for future baseline schema versions."""
    if version == 1:
        return data
    raise BaselineFormatError(f"Unsupported baseline format version for migration: {version}")


def _parse_scenario_identity_from_path(path: str) -> Dict[str, str]:
    stem = os.path.splitext(os.path.basename(path))[0]
    if "_" not in stem or "." not in stem:
        return {}
    func_path, semantic_id = stem.rsplit("_", 1)
    module, function = func_path.rsplit(".", 1)
    if not module or not function or not semantic_id:
        return {}
    return {"module": module, "function": function, "semantic_id": semantic_id}


def _upgrade_legacy_record(data: Dict[str, Any], path: str) -> Dict[str, Any]:
    upgraded = data if ("scenario" in data and "metadata" in data) else {"scenario": data}
    scenario = upgraded.get("scenario")
    if isinstance(scenario, dict):
        scenario.update({k: v for k, v in _parse_scenario_identity_from_path(path).items() if k not in scenario})
        scenario.setdefault("input", {})
        scenario.setdefault("output", {})
    return upgraded


def _normalize_record(data: Dict[str, Any], path: str) -> BaselineRecord:
    data = _upgrade_legacy_record(data, path)
    normalized = {
        "scenario": data.get("scenario", {}),
        "metadata": {**_default_metadata(include_policy_snapshots=False), **data.get("metadata", {})},
        "approval_history": list(data.get("approval_history", [])),
    }
    normalized["metadata"]["format_version"] = int(normalized["metadata"].get("format_version", BASELINE_FORMAT_VERSION))
    normalized = _migrate_record_for_version(normalized, normalized["metadata"]["format_version"])
    if normalized["metadata"]["format_version"] not in SUPPORTED_BASELINE_VERSIONS:
        raise BaselineFormatError(
            f"Unsupported baseline format version: {normalized['metadata']['format_version']} (supported={sorted(SUPPORTED_BASELINE_VERSIONS)})"
        )
    try:
        validate_baseline_record(normalized)
    except ValueError as exc:
        raise BaselineFormatError(str(exc)) from exc
    validate_scenario_schema(normalized["scenario"])
    return normalized


def load_baseline_record(path: str) -> BaselineRecord:
    max_baseline_size = get_config().max_baseline_size
    file_size = os.path.getsize(path)
    if file_size > max_baseline_size:
        raise BaselineFormatError(
            f"Baseline file exceeds maximum allowed size ({max_baseline_size} bytes): {path}"
        )

    with open(path, "r", encoding="utf-8") as handle:
        try:
            raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise BaselineFormatError(f"Invalid JSON in baseline file '{path}': {exc}") from exc
    if not isinstance(raw, dict):
        raise BaselineFormatError(f"Baseline file '{path}' must contain a JSON object at top level")
    return _normalize_record(raw, path)


def save_baseline_record(path: str, record: BaselineRecord) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)


def create_baseline_from_capture(capture_data: Dict[str, Any]) -> BaselineRecord:
    capture_data = {"input": {}, "output": {}, **capture_data}
    capture_data.setdefault("engine_version", __version__)
    return {
        "scenario": capture_data,
        "metadata": {
            **_default_metadata(),
            "approved_at": utcnow_iso(),
            "scenario_status": "approved",
        },
        "approval_history": [{"approved_at": utcnow_iso(), "action": "record"}],
    }


def _decision_id(policy_id: str, action: str, current_status: str) -> str:
    source = f"{policy_id}:{action}:{current_status}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def evaluate_governance_decision(action: str, current_status: str, policy: GovernancePolicy | None = None) -> GovernanceDecision:
    """Return auditable decision details for a governance status transition."""
    policy = policy or resolve_governance_policy()
    if not action or not current_status:
        raise GovernancePolicyError("Governance action and current_status must be non-empty strings")
    transition = policy.transitions.get((action, current_status))
    if transition is None:
        target_state = {
            "approve": "approved",
            "deprecate": "deprecated",
        }.get(action, action)
        if _CUSTOM_TRANSITION_VALIDATOR is not None:
            custom_decision = _CUSTOM_TRANSITION_VALIDATOR(current_status, target_state)
            if isinstance(custom_decision, bool):
                reason_code = "CUSTOM_TRANSITION_ALLOWED" if custom_decision else "CUSTOM_TRANSITION_DENIED"
                explanation = (
                    "Custom governance transition validator allowed transition."
                    if custom_decision
                    else "Custom governance transition validator denied transition."
                )
                return GovernanceDecision(
                    allowed=custom_decision,
                    reason_code=reason_code,
                    human_explanation=explanation,
                    policy_id=policy.policy_id,
                    decision_id=_decision_id(policy.policy_id, action, current_status),
                )
        strict_governance = get_config().strict_governance and STRICT_GOVERNANCE
        if strict_governance:
            raise GovernancePolicyError(f"Invalid governance transition: {action} -> {current_status}")
        return GovernanceDecision(
            allowed=True,
            reason_code="STRICT_GOVERNANCE_DISABLED",
            human_explanation="Unknown governance transition allowed because strict governance is disabled.",
            policy_id=policy.policy_id,
            decision_id=_decision_id(policy.policy_id, action, current_status),
        )
    allowed, reason_code, explanation = transition
    return GovernanceDecision(
        allowed=allowed,
        reason_code=reason_code,
        human_explanation=explanation,
        policy_id=policy.policy_id,
        decision_id=_decision_id(policy.policy_id, action, current_status),
    )


def approve_scenario(path: str, capture_data: Dict[str, Any]) -> BaselineRecord:
    capture_data = {"input": {}, "output": {}, **capture_data}
    capture_data.setdefault("engine_version", __version__)
    record = {"scenario": capture_data, "metadata": _default_metadata(), "approval_history": []} if not os.path.exists(path) else load_baseline_record(path)
    decision = evaluate_governance_decision("approve", record["metadata"].get("scenario_status", "pending"))
    if not decision.allowed:
        raise BaselineFormatError(f"Cannot approve scenario: {decision.reason_code}: {decision.explanation}")
    record["scenario"] = capture_data
    record["metadata"]["approved_at"] = utcnow_iso()
    record["metadata"]["scenario_status"] = "approved"
    record["metadata"]["version_id"] = str(uuid.uuid4())
    record["metadata"]["diff_policy_snapshot"] = diff_policy_snapshot()
    record["metadata"]["governance_policy_snapshot"] = governance_policy_snapshot()
    record["approval_history"].append({"approved_at": utcnow_iso(), "action": "approve"})
    save_baseline_record(path, record)
    return record


def list_scenarios(baseline_dir: str) -> List[Dict[str, Any]]:
    rows = []
    for path in sorted(glob.glob(os.path.join(baseline_dir, "*.json"))):
        record = load_baseline_record(path)
        scenario = record["scenario"]
        rows.append(
            {
                "scenario_id": f"{scenario.get('module')}.{scenario.get('function')}:{scenario.get('semantic_id')}",
                "file": os.path.basename(path),
                "metadata": record["metadata"],
            }
        )
    return rows


def find_scenario_file(baseline_dir: str, scenario_id: str) -> str:
    for path in glob.glob(os.path.join(baseline_dir, "*.json")):
        record = load_baseline_record(path)
        scenario = record["scenario"]
        candidate = f"{scenario.get('module')}.{scenario.get('function')}:{scenario.get('semantic_id')}"
        if candidate == scenario_id:
            return path
    raise ScenarioNotFoundError(f"Scenario '{scenario_id}' not found")


def deprecate_scenario(path: str) -> BaselineRecord:
    record = load_baseline_record(path)
    decision = evaluate_governance_decision("deprecate", record["metadata"].get("scenario_status", "pending"))
    if not decision.allowed:
        raise BaselineFormatError(f"Cannot deprecate scenario: {decision.reason_code}: {decision.explanation}")
    record["metadata"]["scenario_status"] = "deprecated"
    record["approval_history"].append({"approved_at": utcnow_iso(), "action": "deprecate"})
    save_baseline_record(path, record)
    return record
