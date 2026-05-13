"""
SSTMiddleware — zero-decorator HTTP capture for FastAPI and Starlette.

Usage:
    from fastapi import FastAPI
    from sst.middleware import SSTMiddleware

    app = FastAPI()
    app.add_middleware(SSTMiddleware)

    # Restrict to specific path prefixes:
    app.add_middleware(SSTMiddleware, include_paths=["/api/"])

    # Exclude health checks:
    app.add_middleware(SSTMiddleware, exclude_paths=["/health", "/metrics"])

    # Override sampling rate (default: from pyproject.toml / SST_SAMPLING_RATE):
    app.add_middleware(SSTMiddleware, sampling_rate=0.05)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import socket
import threading
import time
from pathlib import Path
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

from . import __version__

logger = logging.getLogger(__name__)
sst_capture_dropped_total = 0
_sst_capture_dropped_lock = threading.Lock()


_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

_STREAMING_CONTENT_TYPES = frozenset(
    {
        "text/event-stream",
        "application/x-ndjson",
        "application/octet-stream",
    }
)

try:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.background import BackgroundTask, BackgroundTasks
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp
except ImportError as _exc:  # pragma: no cover
    raise ImportError(
        "SSTMiddleware requires Starlette. "
        "Install it with: pip install 'sst-python[fastapi]'"
    ) from _exc

from .core import SSTCore, _Fingerprint
from .types import CaptureOutput, CapturePayload


class SSTMiddleware(BaseHTTPMiddleware):
    """Starlette/FastAPI middleware that captures HTTP request/response pairs
    as SST scenarios — no @sst.capture decorators required.

    Every captured request becomes a baseline scenario keyed by a
    deterministic semantic_id derived from method, path, path params,
    query params, and JSON body. Headers are excluded (they contain
    auth tokens and request IDs that are volatile and PII-heavy).

    The captured scenarios are stored in the same .shadow_data/ directory
    and use the same baseline format as @sst.capture, so all existing
    SST CLI commands (sst record, sst verify, sst approve, sst baseline)
    work without modification.

    Note: this class constructs CapturePayload directly rather than going
    through SSTCore._build_payload. That method calls _cached_get_source()
    and _cached_analyze_dependencies() which use lru_cache and require a
    real callable — passing a stub object causes a TypeError that is
    silently swallowed, resulting in captures never being written.
    CapturePayload is a stable public dataclass in sst.types and safe to
    construct directly.

    Limitations:
        - Streaming responses (text/event-stream, application/x-ndjson,
          application/octet-stream) are NOT captured. SST detects them by
          Content-Type and passes through without buffering.
        - Request body is buffered via request.body(). Endpoints reading the
          body via request.stream() may conflict with BaseHTTPMiddleware.
          See: https://www.starlette.io/middleware/#limitations
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        include_paths: Optional[Sequence[str]] = None,
        exclude_paths: Optional[Sequence[str]] = None,
        sampling_rate: Optional[float] = None,
        redact_headers: list[str] | None = None,
        redact_query: list[str] | None = None,
        redact_body: Callable[[dict], dict] | None = None,
    ) -> None:
        super().__init__(app)
        self._core = SSTCore()
        self._dir_probe = _DirSizeProbe(ttl_seconds=5.0)
        self._include_paths: List[str] = list(include_paths or [])
        self._exclude_paths: List[str] = list(exclude_paths or [])
        self._sampling_rate = sampling_rate
        default_headers = ["authorization", "cookie", "set-cookie", "x-api-key"]
        self._redact_headers = {header.lower() for header in (redact_headers or default_headers)}
        self._redact_query = {key.lower() for key in (redact_query or [])}
        self._redact_body = redact_body
        self._pending_counts_lock = threading.Lock()
        self._pending_counts: dict[tuple[str, str], int] = {}
        self._rehydrate_pending_counts()

    def _capture_limit_exceeded(self) -> bool:
        size_bytes, files_count = self._dir_probe.get(self._core.storage_dir)
        config = self._core.config
        return (
            size_bytes > config.max_capture_dir_bytes
            or files_count > config.max_pending_files
        )

    def _should_capture_path(self, path: str) -> bool:
        """Return True if this path should be captured.

        If include_paths is set, path must match at least one prefix.
        If exclude_paths is set, path must not match any prefix.
        If neither is set, all paths are captured.
        """
        if self._exclude_paths:
            for prefix in self._exclude_paths:
                if path.startswith(prefix):
                    return False
        if self._include_paths:
            return any(path.startswith(p) for p in self._include_paths)
        return True

    @staticmethod
    def _parse_json_body(body: bytes, content_type: str) -> object:
        """Return parsed JSON body, or {} for non-JSON / unparseable content."""
        if "application/json" in content_type and body:
            try:
                return json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
        return {}

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _headers_subset_for_hash(self, headers: dict[str, str]) -> dict[str, str]:
        subset: dict[str, str] = {}
        for key, value in headers.items():
            key_lower = key.lower()
            if key_lower in self._redact_headers or key_lower in _HOP_BY_HOP_HEADERS:
                continue
            subset[key_lower] = value
        return subset

    def _compute_request_hash(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        parsed_body: object,
        request_body: bytes,
        content_type: str,
    ) -> str:
        if isinstance(parsed_body, (dict, list, str, int, float, bool)) or parsed_body is None:
            body_for_hash: object = parsed_body
        else:
            body_for_hash = {}

        if "application/json" not in content_type and request_body:
            body_for_hash = {"binary_sha256": hashlib.sha256(request_body).hexdigest()}

        canonical_payload = {
            "method": method,
            "path": path,
            "headers": self._headers_subset_for_hash(headers),
            "body": body_for_hash,
        }
        digest = hashlib.sha256(self._canonical_json(canonical_payload).encode("utf-8")).hexdigest()
        return digest[:16]

    def _rehydrate_pending_counts(self) -> None:
        try:
            storage = Path(self._core.storage_dir)
            if not storage.exists():
                return
            for file_path in storage.glob("*.json"):
                name = file_path.stem
                parts = name.rsplit("_", 4)
                if len(parts) < 5:
                    continue
                semantic_id = parts[-4]
                request_hash = parts[-3]
                if len(semantic_id) != 32 or len(request_hash) != 16:
                    continue
                key = (semantic_id, request_hash)
                self._pending_counts[key] = self._pending_counts.get(key, 0) + 1
        except Exception:
            logger.debug("SST: failed to rehydrate pending counts", exc_info=True)

    def _allow_pending_capture(self, semantic_id: str, request_hash: str) -> bool:
        key = (semantic_id, request_hash)
        with self._pending_counts_lock:
            next_count = self._pending_counts.get(key, 0) + 1
            self._pending_counts[key] = next_count
            return next_count <= 3

    def _write_http_capture(
        self,
        method: str,
        path: str,
        masked_inputs: Dict,
        output_snapshot: CaptureOutput,
        request_hash: str,
    ) -> None:
        """Write one HTTP capture to shadow_dir.

        Constructs CapturePayload directly to avoid lru_cache/inspect
        issues that arise when passing a non-callable stub to _build_payload.
        """
        if self._core.verify_mode:
            return

        os.makedirs(self._core.storage_dir, exist_ok=True)
        semantic_id = _Fingerprint.semantic_hash(masked_inputs)
        now = datetime.now(timezone.utc)
        if not self._allow_pending_capture(semantic_id, request_hash):
            return
        payload = CapturePayload(
            function=f"{method} {path}",
            module="http",
            semantic_id=semantic_id,
            engine_version=__version__,
            timestamp=now.isoformat(),
            input=masked_inputs,
            output=output_snapshot,
            dependencies=[],
            execution_metadata={
                "timestamp": now.isoformat(),
                "python_version": platform.python_version(),
                "hostname": socket.gethostname(),
            },
            dependency_capture={
                "network_calls": {"captured": False, "hook": "stub"},
                "database_calls": {"captured": False, "hook": "stub"},
            },
            source="",
        )
        safe_path = path.replace("/", "_")
        pid = os.getpid()
        filename = (
            f"http.{method}{safe_path}_{semantic_id}_{request_hash}_"
            f"{pid}_"
            f"{now.strftime('%H%M%S_%f')}.json"
        )
        payload_str = json.dumps(
            asdict(payload),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        fpath = Path(self._core.storage_dir) / filename
        fpath.write_text(payload_str, encoding="utf-8")

    @staticmethod
    def _append_background_task(response: Response, task: BackgroundTask) -> None:
        """Append task to response.background without replacing existing tasks."""
        if response.background is None:
            response.background = task
            return
        if isinstance(response.background, BackgroundTasks):
            response.background.add_task(task.func, *task.args, **task.kwargs)
            return

        existing = response.background
        chained = BackgroundTasks()
        chained.add_task(existing.func, *existing.args, **existing.kwargs)
        chained.add_task(task.func, *task.args, **task.kwargs)
        response.background = chained

    def _redact_headers_map(self, headers: dict[str, str]) -> dict[str, str]:
        redacted = dict(headers)
        for key in list(redacted.keys()):
            if key.lower() in self._redact_headers:
                redacted[key] = "***"
        return redacted

    def _redact_query_map(self, query: dict[str, str]) -> dict[str, str]:
        redacted = dict(query)
        for key in list(redacted.keys()):
            if key.lower() in self._redact_query:
                redacted[key] = "***"
        return redacted

    def _redact_body_payload(self, body: object) -> object:
        if isinstance(body, dict) and self._redact_body is not None:
            return self._redact_body(dict(body))
        return body

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if (
            not self._core.capture_enabled
            or not self._core.enabled
            or not self._should_capture_path(path)
            or not self._core._should_sample_capture(self._sampling_rate)
        ):
            return await call_next(request)

        request_body = await request.body()

        content_type = request.headers.get("content-type", "")
        parsed_body = self._parse_json_body(request_body, content_type)
        request_hash = self._compute_request_hash(
            request.method,
            path,
            dict(request.headers),
            parsed_body,
            request_body,
            content_type,
        )
        raw_inputs = {
            "method": request.method,
            "path": path,
            "path_params": dict(request.path_params),
            "query_params": self._redact_query_map(dict(request.query_params)),
            "headers": self._redact_headers_map(dict(request.headers)),
            "body": self._redact_body_payload(parsed_body),
        }
        masked_inputs = self._core._mask_pii(self._core._serialize(raw_inputs))

        response: Response = await call_next(request)

        resp_content_type = (
            response.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        if resp_content_type in _STREAMING_CONTENT_TYPES:
            logger.warning(
                "SST: Skipping capture for %s %s — streaming response (%s) not supported.",
                request.method,
                request.url.path,
                resp_content_type,
            )
            return response

        body_chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            body_chunks.append(chunk)
        response_body = b"".join(body_chunks)

        safe_response = Response(
            content=response_body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
        if response.background is not None:
            safe_response.background = response.background

        def _capture_in_background() -> None:
            try:
                if self._capture_limit_exceeded():
                    global sst_capture_dropped_total
                    with _sst_capture_dropped_lock:
                        sst_capture_dropped_total += 1
                    return
                status_code = response.status_code
                resp_ct = response.headers.get("content-type", "")

                if 200 <= status_code < 300:
                    raw_result = self._parse_json_body(response_body, resp_ct)
                    masked_result = self._core._mask_pii(self._core._serialize(raw_result))
                    output_snapshot: CaptureOutput = {
                        "status": "success",
                        "raw_result": masked_result,
                    }
                else:
                    error_text = response_body.decode("utf-8", errors="replace")[:500]
                    output_snapshot = {
                        "status": "failure",
                        "error_type": f"HTTP_{status_code}",
                        "error": error_text,
                    }

                self._write_http_capture(
                    request.method,
                    path,
                    masked_inputs,
                    output_snapshot,
                    request_hash,
                )
            except Exception:
                logger.exception("sst capture failed")

        self._append_background_task(safe_response, BackgroundTask(_capture_in_background))

        return safe_response


class _DirSizeProbe:
    def __init__(self, ttl_seconds: float = 5.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._last_ts = 0.0
        self._last_path: str | None = None
        self._cached = (0, 0)

    def get(self, dir_path: str | os.PathLike[str]) -> tuple[int, int]:
        now = time.monotonic()
        path_str = os.fspath(dir_path)
        with self._lock:
            if (
                self._last_path == path_str
                and now - self._last_ts <= self._ttl_seconds
            ):
                return self._cached

            total_bytes = 0
            file_count = 0
            try:
                with os.scandir(path_str) as entries:
                    for entry in entries:
                        if not entry.is_file():
                            continue
                        file_count += 1
                        try:
                            total_bytes += entry.stat().st_size
                        except OSError:
                            continue
            except FileNotFoundError:
                total_bytes = 0
                file_count = 0

            self._cached = (total_bytes, file_count)
            self._last_ts = now
            self._last_path = path_str
            return self._cached
