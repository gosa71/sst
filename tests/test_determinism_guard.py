import json
import os

from sst.core import SSTCore


def test_two_captures_have_identical_semantic_id_and_normalized_output(tmp_path):
    storage_a = tmp_path / "run_a"
    storage_b = tmp_path / "run_b"
    baseline_dir = tmp_path / "baseline"
    os.environ["SST_ENABLED"] = "true"
    os.environ["SST_CAPTURE_ENABLED"] = "true"

    core_a = SSTCore(storage_dir=str(storage_a), baseline_dir=str(baseline_dir))
    core_b = SSTCore(storage_dir=str(storage_b), baseline_dir=str(baseline_dir))

    def deterministic_func(x, y):
        return {"sum": x + y, "items": [" b ", "a"]}

    wrapped_a = core_a.capture(deterministic_func)
    wrapped_b = core_b.capture(deterministic_func)

    assert wrapped_a(3, 4) == {"sum": 7, "items": [" b ", "a"]}
    assert wrapped_b(3, 4) == {"sum": 7, "items": [" b ", "a"]}

    file_a = next(storage_a.glob("*.json"))
    file_b = next(storage_b.glob("*.json"))

    payload_a = json.loads(file_a.read_text(encoding="utf-8"))
    payload_b = json.loads(file_b.read_text(encoding="utf-8"))

    assert payload_a["semantic_id"] == payload_b["semantic_id"]
    assert payload_a["input"] == payload_b["input"]
    assert payload_a["output"]["raw_result"] == payload_b["output"]["raw_result"]
