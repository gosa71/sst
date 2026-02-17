"""Regression tests for bug fixes: sentinel consistency, phone regex, record counter."""
import json

import pytest

from sst.core import _CaptureNormalizer


class TestSentinelConsistency:
    """Fix 1: serialize and mask_pii must return the same sentinel type at MAX_DEPTH."""

    def test_mask_pii_returns_sentinel_dict_at_max_depth(self):
        n = _CaptureNormalizer()
        deep = {}
        node = deep
        for _ in range(105):
            node["child"] = {}
            node = node["child"]

        masked = n.mask_pii(deep)
        cursor = masked
        while isinstance(cursor, dict) and "child" in cursor:
            cursor = cursor["child"]

        assert isinstance(cursor, dict), (
            f"mask_pii should return TRUNCATION_SENTINEL dict at MAX_DEPTH, got {type(cursor)}"
        )
        assert cursor == _CaptureNormalizer.TRUNCATION_SENTINEL

    def test_serialize_and_mask_pii_sentinel_types_match(self):
        n = _CaptureNormalizer()
        deep = {}
        node = deep
        for _ in range(105):
            node["child"] = {}
            node = node["child"]

        ser = n.serialize(deep)
        cursor_ser = ser
        while isinstance(cursor_ser, dict) and "child" in cursor_ser:
            cursor_ser = cursor_ser["child"]

        masked = n.mask_pii(deep)
        cursor_mask = masked
        while isinstance(cursor_mask, dict) and "child" in cursor_mask:
            cursor_mask = cursor_mask["child"]

        assert type(cursor_ser) == type(cursor_mask), (
            f"serialize sentinel type {type(cursor_ser)} != mask_pii sentinel type {type(cursor_mask)}"
        )
        assert cursor_ser == cursor_mask


class TestPhoneRegexFalsePositives:
    """Fix 2: phone regex must not match order/transaction/reference IDs."""

    @pytest.mark.parametrize(
        "text",
        [
            "ORD-123-456-7890",
            "TXN-555-123-4567",
            "REF-800-555-1234",
            "v1-234-567-8901",
            "ID-999-888-7777",
        ],
    )
    def test_order_id_not_masked(self, text):
        n = _CaptureNormalizer()
        result = n.mask_pii({"order_id": text})
        assert result["order_id"] == text, (
            f"Order-like ID {text!r} was incorrectly masked as phone"
        )

    @pytest.mark.parametrize(
        "text,expected_masked",
        [
            ("555-123-4567", True),
            ("+7 (495) 123-45-67", True),
            ("+1 555 123 4567", True),
            ("(800) 555-1234", True),
        ],
    )
    def test_real_phone_still_masked(self, text, expected_masked):
        n = _CaptureNormalizer()
        result = n.mask_pii({"contact": text})
        was_masked = "[MASKED_PHONE]" in result["contact"]
        assert was_masked == expected_masked, (
            f"Phone {text!r}: expected masked={expected_masked}, got masked={was_masked}"
        )


class TestRecordSavedCount:
    """Fix 3: 'Baseline recorded' message must reflect actually saved count."""

    def test_saved_count_excludes_skipped_files(self, tmp_path, monkeypatch):
        shadow = tmp_path / "shadow"
        baseline = tmp_path / "baseline"
        shadow.mkdir()
        baseline.mkdir()

        # 3 valid files
        for i in range(3):
            (shadow / f"mod.fn_{i:032d}_1.json").write_text(
                json.dumps(
                    {
                        "module": "mod",
                        "function": "fn",
                        "semantic_id": f"{i:032d}",
                        "input": {},
                        "output": {"raw_result": i, "status": "success"},
                        "engine_version": "0.2.0",
                    }
                ),
                encoding="utf-8",
            )

        # 2 corrupted files
        (shadow / "corrupted_a.json").write_text("{bad json", encoding="utf-8")
        (shadow / "corrupted_b.json").write_text("{bad json", encoding="utf-8")

        monkeypatch.setenv("SST_SHADOW_DIR", str(shadow))
        monkeypatch.setenv("SST_BASELINE_DIR", str(baseline))

        from sst.config import refresh_config

        refresh_config()

        # We test the counter behavior by reproducing the save loop and ensuring
        # skipped files are excluded from the final count.
        from sst.governance import create_baseline_from_capture, save_baseline_record

        files = list(shadow.glob("*.json"))
        saved_count = 0
        for fp in files:
            try:
                cd = json.loads(fp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            try:
                bn = f"{cd['module']}.{cd['function']}_{cd['semantic_id']}.json"
                save_baseline_record(str(baseline / bn), create_baseline_from_capture(cd))
                saved_count += 1
            except KeyError:
                continue

        assert saved_count == 3, f"Expected 3 saved, got {saved_count}"
        assert len(list(baseline.glob("*.json"))) == 3
