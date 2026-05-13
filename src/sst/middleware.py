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

import json
import logging
import os
import platform
import socket
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from . import __version__

logger = logging.getLogger(__name__)

_STREAMING_CONTENT_TYPES = frozenset(
    {
        "text/event-stream",
        "application/x-ndjson",
        "application/octet-stream",
    }
)

try:
    from starlette.middleware.base import BaseHTTPMiddleware
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
    ) -> None:
        super().__init__(app)
        self._core = SSTCore()
        self._include_paths: List[str] = list(include_paths or [])
        self._exclude_paths: List[str] = list(exclude_paths or [])
        self._sampling_rate = sampling_rate

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

    def _write_http_capture(
        self,
        method: str,
        path: str,
        masked_inputs: Dict,
        output_snapshot: CaptureOutput,
    ) -> None:
        """Write one HTTP capture to shadow_dir.

        Constructs CapturePayload directly to avoid lru_cache/inspect
        issues that arise when passing a non-callable stub to _build_payload.
        """
        if self._core.verify_mode:
            return
        try:
            os.makedirs(self._core.storage_dir, exist_ok=True)
            semantic_id = _Fingerprint.semantic_hash(masked_inputs)
            now = datetime.now(timezone.utc)
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
            filename = (
                f"http.{method}{safe_path}_{semantic_id}_"
                f"{now.strftime('%H%M%S_%f')}.json"
            )
            payload_str = json.dumps(
                asdict(payload),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            fpath = os.path.join(self._core.storage_dir, filename)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(payload_str)
        except Exception as write_err:
            logger.warning("SST: Failed to write HTTP capture: %s", write_err)

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
        raw_inputs = {
            "method": request.method,
            "path": path,
            "path_params": dict(request.path_params),
            "query_params": dict(request.query_params),
            "body": self._parse_json_body(request_body, content_type),
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

        response_body = bytearray()
        async for chunk in response.body_iterator:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            response_body.extend(chunk)
        response_body = bytes(response_body)

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

        self._write_http_capture(request.method, path, masked_inputs, output_snapshot)

        return Response(
            content=response_body,
            status_code=status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
