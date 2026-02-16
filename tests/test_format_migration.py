import json

from sst import governance


def test_format_version_migration_from_v1_to_v2(tmp_path, monkeypatch):
    baseline_path = tmp_path / "legacy.json"
    legacy = {
        "scenario": {
            "module": "mod",
            "function": "fn",
            "semantic_id": "sid",
            "input": {},
            "output": {"raw_result": {"value": 1}},
        },
        "metadata": {
            "format_version": 1,
            "version_id": "v1",
            "created_at": "2024-01-01T00:00:00+00:00",
            "approved_at": "2024-01-01T00:00:00+00:00",
            "scenario_status": "approved",
        },
        "approval_history": [{"approved_at": "2024-01-01T00:00:00+00:00", "action": "record"}],
    }
    baseline_path.write_text(json.dumps(legacy), encoding="utf-8")

    original_supported = governance.SUPPORTED_BASELINE_VERSIONS
    monkeypatch.setattr(governance, "SUPPORTED_BASELINE_VERSIONS", {1, 2})

    original_migrate = governance._migrate_record_for_version

    def migrate(data, version):
        if version == 1:
            migrated = data.copy()
            migrated["metadata"] = dict(migrated["metadata"])
            migrated["metadata"]["format_version"] = 2
            migrated["metadata"]["migration_applied"] = "v1_to_v2"
            return migrated
        return original_migrate(data, version)

    monkeypatch.setattr(governance, "_migrate_record_for_version", migrate)

    loaded = governance.load_baseline_record(str(baseline_path))

    assert loaded["metadata"]["format_version"] == 2
    assert loaded["metadata"]["migration_applied"] == "v1_to_v2"
    assert loaded["scenario"]["semantic_id"] == "sid"

    monkeypatch.setattr(governance, "SUPPORTED_BASELINE_VERSIONS", original_supported)
