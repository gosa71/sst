"""
Meta-tests for the SST framework itself.
Tests that SST's core components work correctly.
"""
import pytest
import os
import json
import shutil
import tempfile
from unittest.mock import patch
from sst.config import refresh_config
from sst.core import SSTCore
from sst.governance import create_baseline_from_capture, save_baseline_record


@pytest.fixture
def temp_dirs():
    """Create temporary directories for shadow data and baselines."""
    shadow_dir = tempfile.mkdtemp()
    baseline_dir = tempfile.mkdtemp()
    yield shadow_dir, baseline_dir
    shutil.rmtree(shadow_dir, ignore_errors=True)
    shutil.rmtree(baseline_dir, ignore_errors=True)


@pytest.fixture
def sst_instance(temp_dirs):
    """Create an SSTCore instance with temp directories."""
    shadow_dir, baseline_dir = temp_dirs
    with patch.dict(os.environ, {"SST_ENABLED": "true"}):
        core = SSTCore(storage_dir=shadow_dir, baseline_dir=baseline_dir)
        yield core


class TestPIIMasking:
    def test_masks_email(self, sst_instance):
        data = {"contact": "user@example.com"}
        masked = sst_instance._mask_pii(data)
        assert masked["contact"] == "[MASKED_EMAIL]"

    def test_masks_phone(self, sst_instance):
        data = {"phone": "555-123-4567"}
        masked = sst_instance._mask_pii(data)
        assert "MASKED_PHONE" in masked["phone"]

    def test_masks_ssn(self, sst_instance):
        data = {"ssn": "123-45-6789"}
        masked = sst_instance._mask_pii(data)
        assert "MASKED_SSN" in masked["ssn"]

    def test_masks_sensitive_keys(self, sst_instance):
        data = {"password": "secret123", "api_key": "sk-abc123"}
        masked = sst_instance._mask_pii(data)
        assert masked["password"] == "[MASKED_SENSITIVE_KEY]"
        assert masked["api_key"] == "[MASKED_SENSITIVE_KEY]"

    def test_masks_nested_pii(self, sst_instance):
        data = {"user": {"email": "test@test.com", "name": "Alice"}}
        masked = sst_instance._mask_pii(data)
        assert masked["user"]["email"] == "[MASKED_EMAIL]"
        assert masked["user"]["name"] == "Alice"

    def test_masks_list_pii(self, sst_instance):
        data = ["user1@example.com", "user2@example.com"]
        masked = sst_instance._mask_pii(data)
        assert all("MASKED_EMAIL" in m for m in masked)


class TestSerialization:
    def test_serializes_primitives(self, sst_instance):
        assert sst_instance._serialize("hello") == "hello"
        assert sst_instance._serialize(42) == 42
        assert sst_instance._serialize(3.14) == 3.14
        assert sst_instance._serialize(True) is True
        assert sst_instance._serialize(None) is None

    def test_serializes_dict(self, sst_instance):
        data = {"a": 1, "b": "two"}
        result = sst_instance._serialize(data)
        assert result == {"a": 1, "b": "two"}

    def test_serializes_list(self, sst_instance):
        data = [1, "two", 3.0]
        result = sst_instance._serialize(data)
        assert result == [1, "two", 3.0]

    def test_serializes_custom_object(self, sst_instance):
        class Foo:
            def __init__(self):
                self.x = 10
                self.y = "bar"
        
        result = sst_instance._serialize(Foo())
        assert result["__class__"] == "Foo"
        assert result["x"] == 10
        assert result["y"] == "bar"


class TestSemanticHash:
    def test_same_input_same_hash(self, sst_instance):
        data1 = {"a": 1, "b": 2}
        data2 = {"b": 2, "a": 1}
        assert sst_instance._get_semantic_hash(data1) == sst_instance._get_semantic_hash(data2)

    def test_different_input_different_hash(self, sst_instance):
        data1 = {"a": 1}
        data2 = {"a": 2}
        assert sst_instance._get_semantic_hash(data1) != sst_instance._get_semantic_hash(data2)


class TestCaptureDecorator:
    def test_capture_saves_file(self, sst_instance, temp_dirs):
        shadow_dir, _ = temp_dirs

        @sst_instance.capture
        def add(a, b):
            return a + b

        result = add(2, 3)
        assert result == 5

        files = os.listdir(shadow_dir)
        assert len(files) == 1
        
        with open(os.path.join(shadow_dir, files[0]), "r") as f:
            data = json.load(f)
        
        assert data["function"] == "add"
        assert data["output"]["status"] == "success"

    def test_capture_handles_exception(self, sst_instance, temp_dirs):
        shadow_dir, _ = temp_dirs

        @sst_instance.capture
        def fail():
            raise ValueError("test error")

        with pytest.raises(ValueError):
            fail()

        files = os.listdir(shadow_dir)
        assert len(files) == 1
        
        with open(os.path.join(shadow_dir, files[0]), "r") as f:
            data = json.load(f)
        
        assert data["output"]["status"] == "failure"
        assert data["output"]["error_type"] == "ValueError"


class TestVerifyBaseline:
    def test_no_regression_passes(self, sst_instance, temp_dirs):
        shadow_dir, baseline_dir = temp_dirs
        @sst_instance.capture
        def greet(name):
            return {"message": f"Hello, {name}!"}

        # First call captures to shadow_dir (verify mode off)
        greet("World")

        # Build baseline directly from the captured file using the proper API
        files = list(os.scandir(shadow_dir))
        assert files, "Expected at least one capture file"
        with open(files[0].path, "r") as f:
            cap_data = json.load(f)

        baseline_name = f"{cap_data['module']}.{cap_data['function']}_{cap_data['semantic_id']}.json"
        baseline_record = create_baseline_from_capture(cap_data)
        save_baseline_record(os.path.join(baseline_dir, baseline_name), baseline_record)

        # Verify — same input, same output, should not raise
        with patch.dict(os.environ, {"SST_VERIFY": "true"}):
            greet("World")

    def test_regression_detected(self, sst_instance, temp_dirs):
        shadow_dir, baseline_dir = temp_dirs

        # Create a fake baseline with different output
        baseline_data = {
            "output": {
                "raw_result": {"value": "old_result"},
                "status": "success"
            }
        }

        @sst_instance.capture
        def compute():
            return {"value": "new_result"}

        # Save fake baseline
        masked_inputs = sst_instance._mask_pii(
            sst_instance._serialize({"args": [], "kwargs": {}})
        )
        semantic_id = sst_instance._get_semantic_hash(masked_inputs)
        baseline_path = os.path.join(
            baseline_dir,
            f"test_sst_meta.compute_{semantic_id}.json"
        )
        with open(baseline_path, "w") as f:
            json.dump(baseline_data, f)

        with patch.dict(os.environ, {"SST_VERIFY": "true"}):
            with pytest.raises(RuntimeError, match="REGRESSION DETECTED"):
                compute()

    def test_structured_diff_is_reported(self, sst_instance):
        baseline = {"value": "old", "nested": {"count": 1}}
        current = {"value": "new", "nested": {"count": 2}, "extra": True}

        changes = sst_instance._build_structured_diff(baseline, current)

        assert any(change["path"] == "$.value" and change["change_type"] == "value_changed" for change in changes)
        assert any(change["path"] == "$.nested.count" and change["change_type"] == "value_changed" for change in changes)
        assert any(change["path"] == "$.extra" and change["change_type"] == "added" for change in changes)

    def test_regression_explanation_contains_summary(self, sst_instance):
        changes = [
            {"path": "$.a", "change_type": "value_changed"},
            {"path": "$.b", "change_type": "added"},
        ]

        explanation = sst_instance._explain_regression(changes)

        assert "Detected 2 difference(s)" in explanation
        assert "value_changed=1" in explanation
        assert "added=1" in explanation


def test_capture_respects_decorator_sampling_override(tmp_path):
    with patch.dict(os.environ, {"SST_ENABLED": "true", "SST_SAMPLING_RATE": "1.0"}):
        core = SSTCore(storage_dir=str(tmp_path), baseline_dir=str(tmp_path / "base"))

        @core.capture(sampling_rate=0.0)
        def add(a, b):
            return a + b

        assert add(1, 2) == 3

    assert not list(tmp_path.glob("*.json"))


def test_capture_reads_sampling_rate_from_env(tmp_path):
    with patch.dict(os.environ, {"SST_ENABLED": "true", "SST_SAMPLING_RATE": "0.0"}):
        refresh_config()
        core = SSTCore(storage_dir=str(tmp_path), baseline_dir=str(tmp_path / "base"))

        @core.capture
        def add(a, b):
            return a + b

        assert add(1, 2) == 3

    assert not list(tmp_path.glob("*.json"))


def test_capture_can_be_globally_disabled(tmp_path):
    with patch.dict(os.environ, {"SST_ENABLED": "true", "SST_CAPTURE_ENABLED": "false"}):
        core = SSTCore(storage_dir=str(tmp_path), baseline_dir=str(tmp_path / "base"))

        @core.capture
        def add(a, b):
            return a + b

        assert add(1, 2) == 3

    assert not list(tmp_path.glob("*.json"))
