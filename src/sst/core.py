import ast
import functools
import hashlib
import inspect
import json
import logging
import os
import platform
import random
import re
import socket
import textwrap
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List

from . import __version__
from .config import get_config
from .diff import apply_diff_policy, build_structured_diff, format_human_diff, normalize_for_compare, summarize_changes
from .errors import CaptureContractError, RegressionDetectedError
from .governance import load_baseline_record
from .types import CaptureOutput, CapturePayload

logger = logging.getLogger(__name__)

__all__ = ["SSTCore", "sst"]

MAX_STRING_LENGTH_FOR_REGEX = 10000
_DEP_CACHE_SIZE = 256


@functools.lru_cache(maxsize=_DEP_CACHE_SIZE)
def _cached_get_source(func) -> str:
    try:
        return inspect.getsource(func)
    except OSError:
        return ""


@functools.lru_cache(maxsize=_DEP_CACHE_SIZE)
def _cached_analyze_dependencies(func) -> tuple[str, ...]:
    source = textwrap.dedent(_cached_get_source(func))
    tree = ast.parse(source)
    calls = []

    def get_full_name(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = get_full_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            full_name = get_full_name(node.func)
            if full_name:
                calls.append(full_name)
    return tuple(set(calls))


class _CaptureNormalizer:
    MAX_DEPTH = 100
    TRUNCATION_SENTINEL = {"__sst_truncated__": "MAX_DEPTH_REACHED"}

    def __init__(self, extra_pii_keys=None, strict_pii_matching: bool = True, extra_pii_patterns=None):
        self.pii_patterns = {
            "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
            "card": re.compile(r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13}|6(?:011|5\d{2})\d{12})\b"),
            "phone": re.compile(
                r"(?:"
                r"\+\d{1,3}[\s\-]?\(?\d{1,4}\)?(?:[\s\-]\d{2,5}){1,3}"
                r"|\+\d{1,3}\(\d{3}\)\d{3}[\-]?\d{2}[\-]?\d{2}"
                r"|\(\d{3}\)\s*\d{3}[\s\-]\d{4}"
                r"|(?<![A-Za-z\-])\b\d{3}[\-]\d{3}[\-]\d{4}\b"
                r")"
            ),
            "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
            "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        }
        self.sensitive_keys = {"password", "secret", "token", "api_key", "auth", "key", "credential"}
        if extra_pii_keys:
            self.sensitive_keys.update(k.lower() for k in extra_pii_keys)
        if extra_pii_patterns:
            for entry in extra_pii_patterns:
                label = entry["label"].lower()
                try:
                    self.pii_patterns[label] = re.compile(entry["pattern"])
                except re.error as e:
                    logger.warning("SST: Invalid custom PII pattern '%s': %s", entry["label"], e)
        self.strict_pii_matching = strict_pii_matching

    def _is_sensitive_key(self, key: str) -> bool:
        key_lower = key.lower()
        if self.strict_pii_matching:
            return any(key_lower == sensitive.lower() for sensitive in self.sensitive_keys)
        return any(sensitive.lower() in key_lower for sensitive in self.sensitive_keys)

    def serialize(self, obj: Any, depth: int = 0) -> Any:
        """Recursively serialize obj to a JSON-compatible structure.

        Custom classes can implement ``__sst_serialize__(self) -> Any`` to control
        how their instances are captured. The method should return a JSON-serializable
        value (dict, list, str, int, float, bool, or None).

        Example::

            class Money:
                def __init__(self, amount, currency):
                    self.amount = amount
                    self.currency = currency

                def __sst_serialize__(self):
                    return {"amount": self.amount, "currency": self.currency}
        """
        if depth > self.MAX_DEPTH:
            return self.TRUNCATION_SENTINEL
        if isinstance(obj, (str, int, float, bool, type(None))):
            return obj
        if isinstance(obj, dict):
            return {str(k): self.serialize(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [self.serialize(i, depth + 1) for i in obj]
        if hasattr(obj, "__sst_serialize__"):
            return self.serialize(obj.__sst_serialize__(), depth + 1)
        if hasattr(obj, "__dict__"):
            return {"__class__": obj.__class__.__name__, **self.serialize(obj.__dict__, depth + 1)}
        return {"__class__": obj.__class__.__name__, "__repr__": repr(obj)}

    def mask_pii(self, data: Any, depth: int = 0) -> Any:
        if depth > self.MAX_DEPTH:
            return self.TRUNCATION_SENTINEL
        if isinstance(data, str):
            if len(data) > MAX_STRING_LENGTH_FOR_REGEX:
                logger.debug(
                    "SST: Skipping PII masking for string of length %s (exceeds %s)",
                    len(data),
                    MAX_STRING_LENGTH_FOR_REGEX,
                )
                return data
            for label, pattern in self.pii_patterns.items():
                data = pattern.sub(f"[MASKED_{label.upper()}]", data)
            return data
        if isinstance(data, dict):
            masked_dict = {}
            for k, v in data.items():
                masked_dict[k] = (
                    "[MASKED_SENSITIVE_KEY]"
                    if self._is_sensitive_key(k)
                    else self.mask_pii(v, depth + 1)
                )
            return masked_dict
        if isinstance(data, list):
            return [self.mask_pii(item, depth + 1) for item in data]
        return data


class _Fingerprint:
    MAX_DEPTH = 100

    @staticmethod
    def semantic_hash(data: Any) -> str:
        def canonicalize(obj, depth: int = 0):
            if depth > _Fingerprint.MAX_DEPTH:
                return {"__sst_truncated__": "MAX_DEPTH_REACHED"}
            if isinstance(obj, dict):
                return {k: canonicalize(v, depth + 1) for k, v in sorted(obj.items())}
            if isinstance(obj, list):
                return [canonicalize(i, depth + 1) for i in obj]
            return f"{type(obj).__name__}:{obj}"

        struct_str = json.dumps(canonicalize(data), sort_keys=True)
        return hashlib.sha256(struct_str.encode()).hexdigest()[:32]


class SSTCore:
    def __init__(self, storage_dir=None, baseline_dir=None, env_var="SST_ENABLED"):
        self._config = get_config()
        self.storage_dir = storage_dir or os.getenv("SST_STORAGE_DIR", self._config.shadow_dir)
        self.baseline_dir = baseline_dir or os.getenv("SST_BASELINE_DIR", self._config.baseline_dir)
        self._env_var = env_var
        self._normalizer = _CaptureNormalizer(
            extra_pii_keys=self._config.pii_keys,
            strict_pii_matching=self._config.strict_pii_matching,
            extra_pii_patterns=self._config.pii_patterns,
        )

    @staticmethod
    def _env_truthy(name: str, default: str) -> bool:
        return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}

    def _should_sample_capture(self, sampling_rate: float | None = None) -> bool:
        rate = self._config.sampling_rate if sampling_rate is None else sampling_rate
        clamped_rate = max(0.0, min(1.0, float(rate)))
        return random.random() < clamped_rate

    @property
    def enabled(self):
        return self._env_truthy(self._env_var, "false")

    @property
    def capture_enabled(self):
        return self._env_truthy("SST_CAPTURE_ENABLED", "true")

    @property
    def verify_mode(self):
        return os.getenv("SST_VERIFY", "false").lower() == "true"

    def _serialize(self, obj: Any) -> Any:
        return self._normalizer.serialize(obj)

    def _mask_pii(self, data: Any) -> Any:
        return self._normalizer.mask_pii(data)

    def _build_structured_diff(self, baseline: Any, current: Any, path: str = "$"):
        return build_structured_diff(baseline, current, path)

    def _get_semantic_hash(self, data: Any) -> str:
        """Backward-compatible semantic fingerprint helper."""
        return _Fingerprint.semantic_hash(data)

    def _explain_regression(self, changes):
        return summarize_changes(changes)

    def _verify_against_baseline(self, func, masked_inputs, masked_result):
        semantic_id = _Fingerprint.semantic_hash(masked_inputs)
        baseline_path = os.path.join(self.baseline_dir, f"{func.__module__}.{func.__name__}_{semantic_id}.json")
        if not os.path.exists(baseline_path):
            return

        baseline_record = load_baseline_record(baseline_path)
        if baseline_record["metadata"].get("scenario_status") == "deprecated":
            return
        baseline_output = baseline_record["scenario"].get("output", {}).get("raw_result")

        filtered_result = normalize_for_compare(apply_diff_policy(masked_result))
        filtered_baseline = normalize_for_compare(apply_diff_policy(baseline_output))

        if filtered_result != filtered_baseline:
            structured_diff = self._build_structured_diff(filtered_baseline, filtered_result)
            error_msg = f"\nREGRESSION DETECTED in {func.__module__}.{func.__name__}\n"
            error_msg += f"Semantic ID: {semantic_id}\n"
            error_msg += "Summary:\n" + self._explain_regression(structured_diff) + "\n\n"
            error_msg += "Human-readable diff:\n" + format_human_diff(structured_diff) + "\n\n"
            error_msg += "Structured diff:\n" + json.dumps(structured_diff, indent=2, sort_keys=True)
            error_msg += f"\nTo approve this change, run: sst approve {func.__module__}.{func.__name__} {semantic_id}"
            scenario_id = f"{func.__module__}.{func.__name__}:{semantic_id}"
            raise RegressionDetectedError(message=error_msg, scenario_id=scenario_id)

    def _analyze_dependencies(self, func) -> List[str]:
        try:
            return list(_cached_analyze_dependencies(func))
        except Exception:
            return []

    def _capture_metadata(self) -> Dict[str, str]:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "python_version": platform.python_version(),
            "hostname": socket.gethostname(),
        }

    def _capture_dependency_hooks(self) -> Dict[str, Any]:
        return {
            "network_calls": {"captured": False, "hook": "stub"},
            "database_calls": {"captured": False, "hook": "stub"},
        }

    def _build_payload(self, func, masked_inputs, output_snapshot: CaptureOutput) -> CapturePayload:
        semantic_id = _Fingerprint.semantic_hash(masked_inputs)
        if output_snapshot.get("status") == "success" and "raw_result" not in output_snapshot:
            raise CaptureContractError("Successful capture output is missing required field: raw_result")
        return CapturePayload(
            function=func.__name__,
            module=func.__module__,
            semantic_id=semantic_id,
            engine_version=__version__,
            timestamp=datetime.now(timezone.utc).isoformat(),
            input=masked_inputs,
            output=output_snapshot,
            dependencies=self._analyze_dependencies(func),
            execution_metadata=self._capture_metadata(),
            dependency_capture=self._capture_dependency_hooks(),
            source=_cached_get_source(func),
        )

    def _write_capture(self, func, masked_inputs, output_snapshot):
        try:
            os.makedirs(self.storage_dir, exist_ok=True)
            capture_payload = self._build_payload(func, masked_inputs, output_snapshot)
            filename = (
                f"{capture_payload.module}.{capture_payload.function}_{capture_payload.semantic_id}_"
                f"{datetime.now(timezone.utc).strftime('%H%M%S_%f')}.json"
            )
            with open(os.path.join(self.storage_dir, filename), "w", encoding="utf-8") as f:
                json.dump(asdict(capture_payload), f, indent=2, sort_keys=True)
        except Exception as write_err:
            logger.warning("SST: Failed to write capture data: %s", write_err)

    def capture(self, func=None, *, sampling_rate: float | None = None):
        if func is None:
            return lambda wrapped_func: self.capture(wrapped_func, sampling_rate=sampling_rate)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                if not self.capture_enabled:
                    return await func(*args, **kwargs)
                if not self.enabled and not self.verify_mode:
                    return await func(*args, **kwargs)
                if self.enabled and not self.verify_mode and not self._should_sample_capture(sampling_rate):
                    return await func(*args, **kwargs)

                masked_inputs = self._mask_pii(self._serialize({"args": list(args), "kwargs": kwargs}))
                output_snapshot: CaptureOutput = {"status": "unknown"}
                masked_result = None
                try:
                    result = await func(*args, **kwargs)
                    masked_result = self._mask_pii(self._serialize(result))
                    output_snapshot = {"raw_result": masked_result, "status": "success"}
                    return result
                except Exception as exc:
                    output_snapshot = {"error": str(exc), "error_type": type(exc).__name__, "status": "failure"}
                    raise
                finally:
                    self._write_capture(func, masked_inputs, output_snapshot)
                    if self.verify_mode and output_snapshot.get("status") == "success":
                        self._verify_against_baseline(func, masked_inputs, masked_result)

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not self.capture_enabled:
                return func(*args, **kwargs)
            if not self.enabled and not self.verify_mode:
                return func(*args, **kwargs)
            if self.enabled and not self.verify_mode and not self._should_sample_capture(sampling_rate):
                return func(*args, **kwargs)

            masked_inputs = self._mask_pii(self._serialize({"args": list(args), "kwargs": kwargs}))
            output_snapshot: CaptureOutput = {"status": "unknown"}
            masked_result = None
            try:
                result = func(*args, **kwargs)
                masked_result = self._mask_pii(self._serialize(result))
                output_snapshot = {"raw_result": masked_result, "status": "success"}
                return result
            except Exception as exc:
                output_snapshot = {"error": str(exc), "error_type": type(exc).__name__, "status": "failure"}
                raise
            finally:
                self._write_capture(func, masked_inputs, output_snapshot)
                if self.verify_mode and output_snapshot.get("status") == "success":
                    self._verify_against_baseline(func, masked_inputs, masked_result)

        return wrapper


sst = SSTCore()
