"""Tests for SSTMiddleware — zero-decorator HTTP capture."""

import glob
import json

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
