"""Regression invariants for semantic IDs and PII masking behavior."""

from sst.core import _CaptureNormalizer, _Fingerprint
from sst.diff import apply_diff_policy


def test_semantic_id_type_invariance():
    """Semantic hashes should preserve primitive type distinctions."""
    hash_int = _Fingerprint.semantic_hash({"key": 1})
    hash_str = _Fingerprint.semantic_hash({"key": "1"})

    assert hash_int != hash_str


def test_pii_deep_list_masking():
    """PII masking should recurse into nested list values and structures."""
    normalizer = _CaptureNormalizer()

    payload = {
        "data": [
            "user@example.com",
            "not_sensitive",
            {"nested": ["support@example.com", 123]},
        ]
    }

    masked = normalizer.mask_pii(payload)

    assert masked == {
        "data": [
            "[MASKED_EMAIL]",
            "not_sensitive",
            {"nested": ["[MASKED_EMAIL]", 123]},
        ]
    }


def test_apply_diff_policy_null_safety():
    """Applying diff policy to None should be null-safe and return None."""
    assert apply_diff_policy(None) is None


def test_pii_masking_strict_key_matching_is_exact():
    normalizer = _CaptureNormalizer(strict_pii_matching=True)

    payload = {"monkey": "value", "token": "abc"}
    masked = normalizer.mask_pii(payload)

    assert masked["monkey"] == "value"
    assert masked["token"] == "[MASKED_SENSITIVE_KEY]"


def test_pii_masking_non_strict_allows_substring_matching():
    normalizer = _CaptureNormalizer(strict_pii_matching=False)

    payload = {"monkey": "value", "token": "abc"}
    masked = normalizer.mask_pii(payload)

    assert masked["monkey"] == "[MASKED_SENSITIVE_KEY]"
    assert masked["token"] == "[MASKED_SENSITIVE_KEY]"
