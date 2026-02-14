import json

from sst import __version__
from sst.replay import ReplayEngine


def test_interpreter_contract_golden(tmp_path):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    baseline_file = "tests/golden/interpreter_contract_baseline.json"
    capture_file = "tests/golden/interpreter_contract_capture.json"

    baseline_payload = json.loads(open(baseline_file, encoding="utf-8").read())
    capture_payload = json.loads(open(capture_file, encoding="utf-8").read())

    assert baseline_payload["scenario"]["engine_version"] == __version__

    (baseline_dir / "golden.mod.fn_contract1.json").write_text(
        json.dumps(baseline_payload), encoding="utf-8"
    )
    (capture_dir / "golden.mod.fn_contract1_1.json").write_text(
        json.dumps(capture_payload), encoding="utf-8"
    )

    report = ReplayEngine(str(baseline_dir), str(capture_dir)).replay()

    assert report["regressions"] == []
    assert report["missing"] == []
    assert report["warnings"] == []
    assert report["scenarios"][0]["status"] == "passed"
