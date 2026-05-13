"""Tests for SSTMiddleware — zero-decorator HTTP capture."""

import glob
import json
import time

import pytest

starlette = pytest.importorskip("starlette", reason="starlette not installed")

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def _make_app(middleware_kwargs=None):
    """Build a minimal Starlette app with SSTMiddleware."""
    from sst.middleware import SSTMiddleware

    async def price_endpoint(request: Request):
        body = await request.json()
        product = body.get("product_id", "SKU-001")
        prices = {"SKU-001": 99.9, "SKU-002": 249.0}
        price = prices.get(product, 0.0)
        if price == 0.0:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"product_id": product, "price": price, "currency": "USD"})

    async def health(request: Request):
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/api/price", price_endpoint, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
        ]
    )
    app.add_middleware(SSTMiddleware, **(middleware_kwargs or {}))
    return app


def test_middleware_writes_capture_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    resp = client.post("/api/price", json={"product_id": "SKU-001"})
    assert resp.status_code == 200

    files = glob.glob(str(tmp_path / "*.json"))
    assert len(files) == 1, f"Expected 1 capture file, got {len(files)}"

    data = json.loads(open(files[0], encoding="utf-8").read())
    assert data["function"] == "POST /api/price"
    assert data["module"] == "http"
    assert data["output"]["status"] == "success"
    assert data["output"]["raw_result"]["price"] == 99.9
    assert "Infinity" not in open(files[0], encoding="utf-8").read()


def test_middleware_captures_failure_response(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    resp = client.post("/api/price", json={"product_id": "UNKNOWN"})
    assert resp.status_code == 404

    files = glob.glob(str(tmp_path / "*.json"))
    assert len(files) == 1

    data = json.loads(open(files[0], encoding="utf-8").read())
    assert data["output"]["status"] == "failure"
    assert data["output"]["error_type"] == "HTTP_404"


def test_same_request_produces_same_semantic_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    client.post("/api/price", json={"product_id": "SKU-001"})
    client.post("/api/price", json={"product_id": "SKU-001"})

    files = sorted(glob.glob(str(tmp_path / "*.json")))
    assert len(files) == 2

    ids = [json.loads(open(f, encoding="utf-8").read())["semantic_id"] for f in files]
    assert ids[0] == ids[1], "Same inputs must produce same semantic_id"


def test_different_bodies_produce_different_semantic_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    client.post("/api/price", json={"product_id": "SKU-001"})
    client.post("/api/price", json={"product_id": "SKU-002"})

    files = sorted(glob.glob(str(tmp_path / "*.json")))
    ids = [json.loads(open(f, encoding="utf-8").read())["semantic_id"] for f in files]
    assert ids[0] != ids[1], "Different inputs must produce different semantic_id"


def test_exclude_paths_skips_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app({"exclude_paths": ["/health"]}))
    client.get("/health")

    files = glob.glob(str(tmp_path / "*.json"))
    assert len(files) == 0, "Excluded path must not be captured"


def test_include_paths_filters_correctly(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app({"include_paths": ["/api/"]}))
    client.get("/health")
    client.post("/api/price", json={"product_id": "SKU-001"})

    files = glob.glob(str(tmp_path / "*.json"))
    assert len(files) == 1, "Only /api/ path should be captured"
    data = json.loads(open(files[0], encoding="utf-8").read())
    assert data["function"] == "POST /api/price"


def test_no_capture_when_sst_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "false")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    resp = client.post("/api/price", json={"product_id": "SKU-001"})
    assert resp.status_code == 200

    files = glob.glob(str(tmp_path / "*.json"))
    assert len(files) == 0, "No capture when SST_ENABLED=false"


def test_response_body_unchanged_after_middleware(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    resp = client.post("/api/price", json={"product_id": "SKU-002"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["product_id"] == "SKU-002"
    assert body["price"] == 249.0
    assert body["currency"] == "USD"


def test_pii_masked_in_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    client.post(
        "/api/price",
        json={
            "product_id": "SKU-001",
            "email": "customer@example.com",
        },
    )

    files = glob.glob(str(tmp_path / "*.json"))
    raw = open(files[0], encoding="utf-8").read()
    assert "customer@example.com" not in raw, "Email must be PII-masked in capture"
    assert "MASKED_EMAIL" in raw, "PII masking marker must be present"


def test_capture_filenames_include_pid_and_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())

    monkeypatch.setattr("sst.middleware.os.getpid", lambda: 1001)
    resp_a = client.post("/api/price", json={"product_id": "SKU-001"})
    assert resp_a.status_code == 200

    monkeypatch.setattr("sst.middleware.os.getpid", lambda: 1002)
    resp_b = client.post("/api/price", json={"product_id": "SKU-001"})
    assert resp_b.status_code == 200

    files = sorted(glob.glob(str(tmp_path / "*.json")))
    assert len(files) == 2

    basenames = [f.rsplit("/", 1)[-1] for f in files]
    assert basenames[0] != basenames[1]
    assert "_1001_" in basenames[0] or "_1001_" in basenames[1]
    assert "_1002_" in basenames[0] or "_1002_" in basenames[1]


def test_capture_write_failure_does_not_break_response(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    def _raise_disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("sst.middleware.Path.write_text", _raise_disk_full)

    client = TestClient(_make_app())
    with caplog.at_level("ERROR"):
        resp = client.post("/api/price", json={"product_id": "SKU-001"})

    assert resp.status_code == 200
    assert "sst capture failed" in caplog.text
    assert resp.content == b'{"product_id":"SKU-001","price":99.9,"currency":"USD"}'
    files = glob.glob(str(tmp_path / "*.json"))
    assert files == []


def test_capture_exception_before_write_keeps_response_body(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    monkeypatch.setattr(
        "sst.middleware.SSTMiddleware._parse_json_body",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("boom")),
    )

    client = TestClient(_make_app())
    with caplog.at_level("ERROR"):
        resp = client.post("/api/price", json={"product_id": "SKU-001"})

    assert resp.status_code == 200
    assert resp.content == b'{"product_id":"SKU-001","price":99.9,"currency":"USD"}'
    assert "sst capture failed" in caplog.text


def test_background_task_preserved_on_safe_response(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    from sst.middleware import SSTMiddleware
    from starlette.background import BackgroundTask
    from starlette.responses import Response

    called = {"value": False}

    async def _endpoint(request: Request):
        async def _bg():
            called["value"] = True

        return Response(
            content=b"ok",
            media_type="text/plain",
            background=BackgroundTask(_bg),
        )

    app = Starlette(routes=[Route("/bg", _endpoint, methods=["GET"])])
    app.add_middleware(SSTMiddleware)

    client = TestClient(app)
    resp = client.get("/bg")
    assert resp.status_code == 200
    assert resp.content == b"ok"
    assert called["value"] is True


def test_capture_write_runs_via_background_task(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    marker = {"written": False}
    original = None

    from sst.middleware import SSTMiddleware

    original = SSTMiddleware._write_http_capture

    def _wrapped_write(self, method, path, masked_inputs, output_snapshot, request_hash):
        marker["written"] = True
        return original(self, method, path, masked_inputs, output_snapshot, request_hash)

    monkeypatch.setattr(SSTMiddleware, "_write_http_capture", _wrapped_write)

    client = TestClient(_make_app())
    resp = client.post("/api/price", json={"product_id": "SKU-001"})

    assert resp.status_code == 200
    assert marker["written"] is True
    files = glob.glob(str(tmp_path / "*.json"))
    assert len(files) == 1


def test_sampling_zero_returns_original_response_without_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app({"sampling_rate": 0.0}))
    resp = client.post("/api/price", json={"product_id": "SKU-001"})

    assert resp.status_code == 200
    assert resp.content == b'{"product_id":"SKU-001","price":99.9,"currency":"USD"}'
    files = glob.glob(str(tmp_path / "*.json"))
    assert files == []


def test_middleware_hot_path_p95_regression_budget(monkeypatch, tmp_path):
    if "GITHUB_ACTIONS" in __import__("os").environ:
        pytest.skip("skip potentially flaky microbenchmark in CI")

    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path / "with_sst"))

    async def _endpoint(request: Request):
        return JSONResponse({"ok": True})

    app_with = Starlette(routes=[Route("/ping", _endpoint, methods=["GET"])])
    from sst.middleware import SSTMiddleware

    app_with.add_middleware(SSTMiddleware)
    client_with = TestClient(app_with)

    app_without = Starlette(routes=[Route("/ping", _endpoint, methods=["GET"])])
    client_without = TestClient(app_without)

    def _measure(client):
        samples = []
        for _ in range(10_000):
            t0 = time.perf_counter_ns()
            resp = client.get("/ping")
            assert resp.status_code == 200
            samples.append((time.perf_counter_ns() - t0) / 1_000_000)
        samples.sort()
        return samples[int(len(samples) * 0.95)]

    p95_without = _measure(client_without)
    p95_with = _measure(client_with)

    assert p95_with - p95_without <= 2.0


def test_default_header_redaction_masks_authorization_and_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    resp = client.post(
        "/api/price",
        json={"product_id": "SKU-001"},
        headers={"Authorization": "Bearer secret", "cookie": "sid=abc"},
    )
    assert resp.status_code == 200

    files = glob.glob(str(tmp_path / "*.json"))
    assert len(files) == 1
    data = json.loads(open(files[0], encoding="utf-8").read())
    assert data["input"]["headers"]["authorization"] == "***"
    assert data["input"]["headers"]["cookie"] == "***"


def test_redact_body_callable_removes_password_field(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    def _redact_body(payload: dict) -> dict:
        cleaned = dict(payload)
        cleaned.pop("password", None)
        return cleaned

    client = TestClient(_make_app({"redact_body": _redact_body}))
    resp = client.post("/api/price", json={"product_id": "SKU-001", "password": "secret"})
    assert resp.status_code == 200

    files = glob.glob(str(tmp_path / "*.json"))
    assert len(files) == 1
    data = json.loads(open(files[0], encoding="utf-8").read())
    assert "password" not in data["input"]["body"]


def test_authorization_header_is_redacted_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    resp = client.post("/api/price", json={"product_id": "SKU-001"}, headers={"AUTHORIZATION": "Bearer secret"})
    assert resp.status_code == 200

    files = glob.glob(str(tmp_path / "*.json"))
    assert len(files) == 1
    data = json.loads(open(files[0], encoding="utf-8").read())
    assert data["input"]["headers"]["authorization"] == "***"


def test_capture_dropped_when_pending_files_limit_exceeded(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("SST_MAX_PENDING_FILES", "1")

    (tmp_path / "existing-a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "existing-b.json").write_text("{}", encoding="utf-8")

    import sst.middleware as middleware_mod
    from sst.config import refresh_config

    refresh_config()
    middleware_mod.sst_capture_dropped_total = 0

    client = TestClient(_make_app())
    for _ in range(5):
        resp = client.post("/api/price", json={"product_id": "SKU-001"})
        assert resp.status_code == 200

    files = sorted(glob.glob(str(tmp_path / "*.json")))
    assert len(files) == 2
    assert middleware_mod.sst_capture_dropped_total == 5


def test_dir_size_probe_ttl_limits_scandir_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("SST_MAX_PENDING_FILES", "100000")
    monkeypatch.setenv("SST_MAX_CAPTURE_DIR_BYTES", str(10**9))

    import sst.middleware as middleware_mod
    from sst.config import refresh_config

    refresh_config()
    middleware_mod.sst_capture_dropped_total = 0
    calls = {"n": 0}
    original_scandir = middleware_mod.os.scandir

    def _counting_scandir(path):
        calls["n"] += 1
        return original_scandir(path)

    monkeypatch.setattr(middleware_mod.os, "scandir", _counting_scandir)

    client = TestClient(_make_app())
    for _ in range(100):
        resp = client.post("/api/price", json={"product_id": "SKU-001"})
        assert resp.status_code == 200

    assert calls["n"] <= 2


def test_pending_dedup_caps_identical_requests_at_three(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    for _ in range(1000):
        resp = client.post("/api/price", json={"product_id": "SKU-001"})
        assert resp.status_code == 200

    files = sorted(glob.glob(str(tmp_path / "*.json")))
    assert len(files) <= 3


def test_pending_dedup_uses_separate_counters_for_different_bodies(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    client = TestClient(_make_app())
    for _ in range(1000):
        resp = client.post("/api/price", json={"product_id": "SKU-001"})
        assert resp.status_code == 200
    for _ in range(1000):
        resp = client.post("/api/price", json={"product_id": "SKU-002"})
        assert resp.status_code == 200

    files = sorted(glob.glob(str(tmp_path / "*.json")))
    assert len(files) <= 6

    grouped = {}
    for file_path in files:
        name = file_path.rsplit("/", 1)[-1]
        parts = name.rsplit("_", 5)
        assert len(parts) >= 6
        semantic_id = parts[-5]
        request_hash = parts[-4]
        grouped.setdefault((semantic_id, request_hash), 0)
        grouped[(semantic_id, request_hash)] += 1

    assert len(grouped) == 2
    assert all(count <= 3 for count in grouped.values())




def test_rehydrate_pending_counts_parses_semantic_id_and_request_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    semantic_id = "a" * 32
    request_hash = "b" * 16
    filename = (
        f"http.POST_api_price_{semantic_id}_{request_hash}_"
        "1234_120000_123456.json"
    )
    (tmp_path / filename).write_text("{}", encoding="utf-8")

    from sst.middleware import SSTMiddleware

    instance = SSTMiddleware(app=Starlette())
    assert instance._pending_counts[(semantic_id, request_hash)] == 1

def test_pending_reservation_released_on_write_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SST_ENABLED", "true")
    monkeypatch.setenv("SST_STORAGE_DIR", str(tmp_path))

    calls = {"n": 0}

    def _flaky_write(self, method, path, masked_inputs, output_snapshot, request_hash):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError("disk full")
        return _orig_write(self, method, path, masked_inputs, output_snapshot, request_hash)

    from sst.middleware import SSTMiddleware

    _orig_write = SSTMiddleware._write_http_capture
    monkeypatch.setattr(SSTMiddleware, "_write_http_capture", _flaky_write)

    client = TestClient(_make_app())
    for _ in range(5):
        resp = client.post("/api/price", json={"product_id": "SKU-001"})
        assert resp.status_code == 200

    files = sorted(glob.glob(str(tmp_path / "*.json")))
    assert len(files) == 3
