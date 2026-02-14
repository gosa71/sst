from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable


DEFAULT_DIFF_POLICY: Dict[str, Any] = {
    "ignored_fields": ["timestamp", "transaction_id", "id", "uuid", "duration", "created_at", "approved_at"],
    "ignored_paths": [],
    "list_sort_paths": [],
    "float_tolerance": 1e-6,
    "mask_timestamps": True,
    "mask_uuid_like": False,
    "normalize_string_whitespace": True,
}


@dataclass(frozen=True)
class Config:
    baseline_dir: str = ".sst_baseline"
    shadow_dir: str = ".shadow_data"
    sampling_rate: float = 1.0
    pii_keys: list[str] = field(default_factory=list)
    diff_policy: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_DIFF_POLICY))
    governance_policy: str = "default"
    strict_governance: bool = True
    max_baseline_size: int = 50 * 1024 * 1024
    clean_shadow_on_record: bool = False
    strict_pii_matching: bool = True


_ENV_PREFIX = "SST_"


def _find_pyproject(start_dir: Path) -> Path | None:
    for directory in (start_dir, *start_dir.parents):
        candidate = directory / "pyproject.toml"
        if candidate.exists():
            return candidate
    return None


def _load_toml(path: Path) -> Dict[str, Any]:
    try:
        import tomllib  # py311+

        return tomllib.loads(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError:
        try:
            import tomli

            return tomli.loads(path.read_text(encoding="utf-8"))
        except ModuleNotFoundError:
            try:
                import rtoml

                return rtoml.loads(path.read_text(encoding="utf-8"))
            except ModuleNotFoundError:
                return {}


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_list(value: Any, default: Iterable[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item) for item in value]
    return list(default)


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any] | None) -> Dict[str, Any]:
    merged = dict(base)
    if not isinstance(patch, dict):
        return merged
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _from_sources(raw: Dict[str, Any]) -> Config:
    diff_policy = _deep_merge(DEFAULT_DIFF_POLICY, raw.get("diff_policy"))

    baseline_dir = os.getenv(f"{_ENV_PREFIX}BASELINE_DIR", raw.get("baseline_dir", ".sst_baseline"))
    shadow_dir = os.getenv(f"{_ENV_PREFIX}SHADOW_DIR", raw.get("shadow_dir", ".shadow_data"))
    sampling_rate = _to_float(os.getenv(f"{_ENV_PREFIX}SAMPLING_RATE", raw.get("sampling_rate", 1.0)), 1.0)
    pii_keys = _to_list(os.getenv(f"{_ENV_PREFIX}PII_KEYS", raw.get("pii_keys", [])), [])
    governance_policy = os.getenv(f"{_ENV_PREFIX}GOVERNANCE_POLICY", raw.get("governance_policy", "default"))
    strict_governance = _to_bool(os.getenv(f"{_ENV_PREFIX}STRICT_GOVERNANCE", raw.get("strict_governance", True)), True)
    max_baseline_size = _to_int(raw.get("max_baseline_size", 50 * 1024 * 1024), 50 * 1024 * 1024)
    clean_shadow_on_record = _to_bool(
        os.getenv(f"{_ENV_PREFIX}CLEAN_SHADOW_ON_RECORD", raw.get("clean_shadow_on_record", False)), False
    )
    strict_pii_matching = _to_bool(
        os.getenv(f"{_ENV_PREFIX}STRICT_PII_MATCHING", raw.get("strict_pii_matching", True)), True
    )

    env_ignored = os.getenv(f"{_ENV_PREFIX}DIFF_IGNORED_FIELDS")
    if env_ignored is not None:
        diff_policy["ignored_fields"] = _to_list(env_ignored, diff_policy.get("ignored_fields", []))
    diff_policy["float_tolerance"] = _to_float(
        os.getenv(f"{_ENV_PREFIX}DIFF_FLOAT_TOLERANCE", diff_policy.get("float_tolerance", 1e-6)), 1e-6
    )
    diff_policy["mask_timestamps"] = _to_bool(diff_policy.get("mask_timestamps", True), True)
    diff_policy["mask_uuid_like"] = _to_bool(diff_policy.get("mask_uuid_like", False), False)
    diff_policy["normalize_string_whitespace"] = _to_bool(diff_policy.get("normalize_string_whitespace", True), True)
    diff_policy["ignored_paths"] = _to_list(diff_policy.get("ignored_paths", []), [])
    diff_policy["list_sort_paths"] = _to_list(diff_policy.get("list_sort_paths", []), [])

    return Config(
        baseline_dir=str(baseline_dir),
        shadow_dir=str(shadow_dir),
        sampling_rate=max(0.0, min(1.0, sampling_rate)),
        pii_keys=pii_keys,
        diff_policy=diff_policy,
        governance_policy=str(governance_policy),
        strict_governance=strict_governance,
        max_baseline_size=max(1, max_baseline_size),
        clean_shadow_on_record=clean_shadow_on_record,
        strict_pii_matching=strict_pii_matching,
    )


@lru_cache(maxsize=32)
def load_config(start_dir: str | os.PathLike[str] | None = None) -> Config:
    root = Path(start_dir or os.getcwd()).resolve()
    pyproject = _find_pyproject(root)
    if pyproject is None:
        return _from_sources({})

    parsed = _load_toml(pyproject)
    tool = parsed.get("tool", {}) if isinstance(parsed, dict) else {}
    sst = tool.get("sst", {}) if isinstance(tool, dict) else {}
    return _from_sources(sst if isinstance(sst, dict) else {})


def get_config() -> Config:
    return load_config(os.getcwd())


def refresh_config(start_dir: str | os.PathLike[str] | None = None) -> Config:
    load_config.cache_clear()
    return load_config(start_dir)
