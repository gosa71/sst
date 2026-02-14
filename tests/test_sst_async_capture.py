import asyncio
import json
import os
from unittest.mock import patch

import pytest

from sst.core import SSTCore


def test_async_capture_supports_coroutines(tmp_path):
    with patch.dict(os.environ, {"SST_ENABLED": "true"}):
        core = SSTCore(storage_dir=str(tmp_path), baseline_dir=str(tmp_path / "base"))

        @core.capture
        async def add(a, b):
            await asyncio.sleep(0)
            return {"sum": a + b}

        assert asyncio.run(add(2, 3)) == {"sum": 5}

    files = list(tmp_path.glob("*.json"))
    assert files
    payload = json.loads(files[0].read_text())
    assert payload["execution_metadata"]["python_version"]
    assert "network_calls" in payload["dependency_capture"]


def test_async_capture_handles_exception(tmp_path):
    with patch.dict(os.environ, {"SST_ENABLED": "true"}):
        core = SSTCore(storage_dir=str(tmp_path), baseline_dir=str(tmp_path / "base"))

        @core.capture
        async def fail():
            await asyncio.sleep(0)
            raise ValueError("test error")

        with pytest.raises(ValueError):
            asyncio.run(fail())

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["output"]["status"] == "failure"
    assert payload["output"]["error_type"] == "ValueError"
