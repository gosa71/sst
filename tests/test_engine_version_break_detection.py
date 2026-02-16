import json

import pytest

from sst.governance import create_baseline_from_capture
from sst.replay import ReplayEngine


def test_replay_detects_engine_normalization_break(tmp_path, monkeypatch):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    capture = {
        "module": "mod",
        "function": "fn",
        "semantic_id": "stable-id",
        "output": {"raw_result": {"items": ["a", "b", "c"]}},
    }

    baseline_record = create_baseline_from_capture(capture)
    current_record = create_baseline_from_capture(capture)

    (baseline_dir / "mod.fn_stable-id.json").write_text(json.dumps(baseline_record), encoding="utf-8")
    (capture_dir / "mod.fn_stable-id_1.json").write_text(json.dumps(current_record), encoding="utf-8")

    engine = ReplayEngine(str(baseline_dir), str(capture_dir))

    calls = {"n": 0}

    def broken_normalize(value):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            return {"items": ["BROKEN"]}
        return value

    monkeypatch.setattr(engine, "normalize_output", broken_normalize)

    report = engine.replay()

    assert len(report["regressions"]) == 1
    assert report["regressions"][0]["scenario_id"] == "mod.fn:stable-id"
    assert report["regressions"][0]["changes"]


@pytest.mark.parametrize("engine_version", ["0.0.1", "999.999.999"])
def test_replay_warns_on_engine_version_drift(tmp_path, engine_version):
    baseline_dir = tmp_path / "baseline"
    capture_dir = tmp_path / "capture"
    baseline_dir.mkdir()
    capture_dir.mkdir()

    capture = {
        "module": "mod",
        "function": "fn",
        "semantic_id": "versioned",
        "engine_version": engine_version,
        "output": {"raw_result": {"value": 1}},
    }

    (baseline_dir / "mod.fn_versioned.json").write_text(
        json.dumps(create_baseline_from_capture(capture)),
        encoding="utf-8",
    )
    (capture_dir / "mod.fn_versioned_1.json").write_text(
        json.dumps(create_baseline_from_capture({**capture, "engine_version": "0.2.0"})),
        encoding="utf-8",
    )

    report = ReplayEngine(str(baseline_dir), str(capture_dir)).replay()

    assert report["regressions"] == []
    assert any("potential reinterpretation risk" in warning for warning in report["warnings"])
