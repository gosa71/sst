import asyncio
import json
import os

import pytest

from sst.core import SSTCore


@pytest.mark.asyncio
def test_parallel_capture_consistency(tmp_path):
    os.environ["SST_ENABLED"] = "true"
    os.environ["SST_CAPTURE_ENABLED"] = "true"

    storage = tmp_path / "shadow"
    core = SSTCore(storage_dir=str(storage), baseline_dir=str(tmp_path / "baseline"))

    @core.capture
    async def stable(value):
        await asyncio.sleep(0)
        return {"value": value, "items": [1, 2, 3]}

    async def _run_all():
        return await asyncio.gather(*[stable(42) for _ in range(10)])

    results = asyncio.run(_run_all())
    assert results == [{"value": 42, "items": [1, 2, 3]}] * 10

    artifacts = sorted(storage.glob("*.json"))
    assert len(artifacts) == 10

    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in artifacts]
    semantic_ids = {payload["semantic_id"] for payload in payloads}
    inputs = {json.dumps(payload["input"], sort_keys=True) for payload in payloads}
    outputs = {json.dumps(payload["output"]["raw_result"], sort_keys=True) for payload in payloads}

    assert len(semantic_ids) == 1
    assert len(inputs) == 1
    assert len(outputs) == 1
