"""Reusable, versioned diff utilities for SST behavior regression checks.

Diff semantics (version ``1``):
- Any type mismatch is a high-severity change.
- Any value mismatch for primitives is a medium-severity change.
- Added/removed keys are high-severity changes.
- List length changes are medium-severity changes.

Dynamic field suppression is policy driven and explicit via ``DiffPolicy``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Set

from .config import get_config
from .errors import DiffContractError
from .types import DiffChange

MAX_DIFF_DEPTH = 1000
MAX_DEPTH = 100

_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-"
    r"[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class DiffPolicy:
    """Declarative, auditable diff policy.

    ``ignored_fields`` must be explicit; no hidden suppression is permitted.
    """

    policy_id: str
    semantics_version: int
    ignored_fields: Set[str]


DEFAULT_DIFF_POLICY = DiffPolicy(
    policy_id="default-v1",
    semantics_version=1,
    ignored_fields={"timestamp", "transaction_id", "id", "uuid", "duration", "created_at", "approved_at"},
)


def current_diff_policy(policy: DiffPolicy = DEFAULT_DIFF_POLICY) -> DiffPolicy:
    """Resolve runtime effective diff policy, including configured ignored fields."""
    cfg_policy = get_config().diff_policy
    if policy is DEFAULT_DIFF_POLICY:
        return DiffPolicy(
            policy_id="configured-v1",
            semantics_version=1,
            ignored_fields={field.lower() for field in cfg_policy.get("ignored_fields", DEFAULT_DIFF_POLICY.ignored_fields)},
        )
    return policy


def diff_policy_snapshot(policy: DiffPolicy | None = None) -> Dict[str, Any]:
    """Serializable snapshot for baseline metadata to detect policy drift."""
    effective = current_diff_policy(policy or DEFAULT_DIFF_POLICY)
    cfg_policy = get_config().diff_policy
    config = {
        "ignored_fields": sorted(effective.ignored_fields),
        "ignored_paths": [str(path).strip() for path in cfg_policy.get("ignored_paths", []) if str(path).strip()],
        "list_sort_paths": [str(path).strip() for path in cfg_policy.get("list_sort_paths", []) if str(path).strip()],
        "float_tolerance": float(cfg_policy.get("float_tolerance", 1e-6)),
        "mask_timestamps": bool(cfg_policy.get("mask_timestamps", True)),
        "mask_uuid_like": bool(cfg_policy.get("mask_uuid_like", False)),
        "normalize_string_whitespace": bool(cfg_policy.get("normalize_string_whitespace", True)),
    }
    payload = {
        "policy_id": effective.policy_id,
        "semantics_version": effective.semantics_version,
        "config": config,
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload["hash"] = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return payload


def apply_diff_policy(data: Any, policy: DiffPolicy = DEFAULT_DIFF_POLICY, depth: int = 0) -> Any:
    """Return a copy of *data* with fields ignored by ``policy`` removed."""
    if depth > MAX_DEPTH:
        return "[MAX_DEPTH_REACHED]"
    if data is None:
        return None

    cfg_policy = get_config().diff_policy
    ignored_paths = {str(path).strip() for path in cfg_policy.get("ignored_paths", []) if str(path).strip()}

    def _matches_ignored_path(path: str) -> bool:
        if path in ignored_paths:
            return True
        if path.startswith("$.") and path[2:] in ignored_paths:
            return True
        if path.startswith("$") and path[1:] in ignored_paths:
            return True
        return False

    def _apply(value: Any, path: str, current_depth: int) -> Any:
        if current_depth > MAX_DEPTH:
            return "[MAX_DEPTH_REACHED]"
        if _matches_ignored_path(path):
            return None
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                key_path = f"{path}.{key}"
                if key.lower() in policy.ignored_fields or _matches_ignored_path(key_path):
                    continue
                result[key] = _apply(child, key_path, current_depth + 1)
            return result
        if isinstance(value, list):
            result_list = []
            for index, item in enumerate(value):
                item_path = f"{path}[{index}]"
                if _matches_ignored_path(item_path):
                    continue
                result_list.append(_apply(item, item_path, current_depth + 1))
            return result_list
        return value

    policy = current_diff_policy(policy)
    if not isinstance(policy.semantics_version, int):
        raise DiffContractError("Diff policy semantics_version must be an integer")
    if _matches_ignored_path("$"):
        return None
    return _apply(data, "$", depth)


def filter_dynamic_fields(data: Any) -> Any:
    """Backward compatible alias for explicit policy application."""
    return apply_diff_policy(data, DEFAULT_DIFF_POLICY)


def normalize_for_compare(data: Any, path: str = "$") -> Any:
    """Normalize payloads into deterministic structures for stable comparison."""
    cfg_policy = get_config().diff_policy
    float_tolerance = float(cfg_policy.get("float_tolerance", 1e-6))
    decimals = 6 if float_tolerance <= 0 else max(0, min(12, abs(int(round(-math.log10(float_tolerance))))))
    mask_timestamps = bool(cfg_policy.get("mask_timestamps", True))
    mask_uuid_like = bool(cfg_policy.get("mask_uuid_like", False))
    normalize_string_whitespace = bool(cfg_policy.get("normalize_string_whitespace", True))
    list_sort_paths = {str(item).strip() for item in cfg_policy.get("list_sort_paths", []) if str(item).strip()}

    def _should_sort_list(current_path: str) -> bool:
        if current_path in list_sort_paths:
            return True
        if current_path.startswith("$.") and current_path[2:] in list_sort_paths:
            return True
        if current_path.startswith("$") and current_path[1:] in list_sort_paths:
            return True
        return False

    def _canonical_sort_key(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    if isinstance(data, dict):
        return {key: normalize_for_compare(data[key], f"{path}.{key}") for key in sorted(data.keys())}
    if isinstance(data, list):
        normalized_list = [normalize_for_compare(item, f"{path}[{index}]") for index, item in enumerate(data)]
        if _should_sort_list(path):
            return sorted(normalized_list, key=_canonical_sort_key)
        return normalized_list
    if isinstance(data, float):
        if float_tolerance == 0:
            return data
        return round(data, decimals)
    if isinstance(data, str):
        if normalize_string_whitespace:
            data = " ".join(data.split())
        if mask_timestamps and _ISO_TS_RE.match(data):
            try:
                datetime.fromisoformat(data.replace("Z", "+00:00"))
                return "<timestamp>"
            except ValueError:
                return data
        if mask_uuid_like and _UUID_RE.match(data):
            return "<uuid>"
    return data


def _severity(change_type: str) -> str:
    mapping = {
        "type_changed": "high",
        "added": "high",
        "removed": "high",
        "length_changed": "medium",
        "value_changed": "medium",
    }
    return mapping.get(change_type, "low")


def build_structured_diff(baseline: Any, current: Any, path: str = "$", depth: int = 0) -> List[DiffChange]:
    """Build a path-aware deep diff for dict/list/primitive JSON-like objects."""
    max_depth = min(MAX_DIFF_DEPTH, max(1, sys.getrecursionlimit() - 100))
    if depth > max_depth:
        raise ValueError("Maximum diff depth exceeded")

    changes: List[DiffChange] = []

    if type(baseline) is not type(current):
        changes.append(
            {
                "path": path,
                "change_type": "type_changed",
                "severity": _severity("type_changed"),
                "baseline": baseline,
                "current": current,
                "baseline_type": type(baseline).__name__,
                "current_type": type(current).__name__,
            }
        )
        return changes

    if isinstance(baseline, dict):
        baseline_keys = set(baseline.keys())
        current_keys = set(current.keys())

        for key in sorted(baseline_keys - current_keys):
            changes.append(
                {
                    "path": f"{path}.{key}",
                    "change_type": "removed",
                    "severity": _severity("removed"),
                    "baseline": baseline[key],
                    "current": None,
                }
            )

        for key in sorted(current_keys - baseline_keys):
            changes.append(
                {
                    "path": f"{path}.{key}",
                    "change_type": "added",
                    "severity": _severity("added"),
                    "baseline": None,
                    "current": current[key],
                }
            )

        for key in sorted(baseline_keys & current_keys):
            changes.extend(build_structured_diff(baseline[key], current[key], f"{path}.{key}", depth + 1))

        return changes

    if isinstance(baseline, list):
        for index in range(min(len(baseline), len(current))):
            changes.extend(build_structured_diff(baseline[index], current[index], f"{path}[{index}]", depth + 1))

        if len(baseline) != len(current):
            changes.append(
                {
                    "path": path,
                    "change_type": "length_changed",
                    "severity": _severity("length_changed"),
                    "baseline": len(baseline),
                    "current": len(current),
                }
            )
        return changes

    if baseline != current:
        changes.append(
            {
                "path": path,
                "change_type": "value_changed",
                "severity": _severity("value_changed"),
                "baseline": baseline,
                "current": current,
            }
        )

    return changes


def summarize_changes(changes: List[DiffChange]) -> str:
    """Produce a deterministic short summary of changes."""
    if not changes:
        return "No semantic differences detected."

    counters = Counter(change["change_type"] for change in changes)
    severities = Counter(change.get("severity", "low") for change in changes)
    counters_text = ", ".join(f"{name}={count}" for name, count in sorted(counters.items()))
    severity_text = ", ".join(f"{name}={count}" for name, count in sorted(severities.items()))
    top_paths = ", ".join(change["path"] for change in changes[:3])
    return f"Detected {len(changes)} difference(s): {counters_text}; severity: {severity_text}. Most impacted paths: {top_paths}."


def format_human_diff(changes: List[DiffChange]) -> str:
    """Render a concise human-readable diff from structured changes."""
    if not changes:
        return "No differences."

    lines = []
    for change in changes:
        kind = change["change_type"]
        path = change["path"]
        severity = change.get("severity", "low")
        if kind == "value_changed":
            lines.append(f"~ [{severity}] {path}: {change['baseline']!r} -> {change['current']!r}")
        elif kind == "type_changed":
            lines.append(
                f"~ [{severity}] {path}: type {change['baseline_type']} -> {change['current_type']} "
                f"({change['baseline']!r} -> {change['current']!r})"
            )
        elif kind == "added":
            lines.append(f"+ [{severity}] {path}: {change['current']!r}")
        elif kind == "removed":
            lines.append(f"- [{severity}] {path}: {change['baseline']!r}")
        elif kind == "length_changed":
            lines.append(f"~ [{severity}] {path}: length {change['baseline']} -> {change['current']}")
        else:
            lines.append(f"~ [{severity}] {path}: {json.dumps(change, sort_keys=True)}")

    # Detect reorder pattern: value_changed changes where
    # the set of baseline values == the set of current values
    value_changed = [c for c in changes if c["change_type"] == "value_changed"]
    if value_changed:
        try:
            baseline_vals = {
                json.dumps(c["baseline"], sort_keys=True, default=str)
                for c in value_changed
            }
            current_vals = {
                json.dumps(c["current"], sort_keys=True, default=str)
                for c in value_changed
            }
            if baseline_vals == current_vals:
                # Extract common path prefix (e.g. "$.types" from "$.types[0]", "$.types[1]")
                import re

                paths = [c["path"] for c in value_changed]
                # strip trailing [N] to get parent path
                parent_paths = list(dict.fromkeys(re.sub(r"\[\d+\]$", "", p) for p in paths))
                sort_hint = ", ".join(f'"{p}"' for p in parent_paths)
                lines.append(
                    f"\nHint: values are identical but order differs — this may be non-deterministic ordering.\n"
                    f"If order does not matter, add to [tool.sst.diff_policy] in pyproject.toml:\n"
                    f"  list_sort_paths = [{sort_hint}]\n"
                    f"If order matters, check for non-determinism in your code: "
                    f"set(), dict (pre-3.7), os.listdir(), DB queries without ORDER BY, parallel tasks."
                )
        except (TypeError, ValueError):
            pass
    return "\n".join(lines)
