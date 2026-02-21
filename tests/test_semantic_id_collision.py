import random

from sst.core import _CaptureNormalizer, _Fingerprint


def test_semantic_id_uniqueness_seeded_bulk():
    rnd = random.Random(20240131)
    seen = set()

    for i in range(2000):
        payload = {
            "args": [rnd.randint(0, 10**9), f"user-{i}", {"nested": rnd.random()}],
            "kwargs": {"index": i, "flag": bool(i % 2)},
        }
        sid = _Fingerprint.semantic_hash(payload)
        assert sid not in seen
        seen.add(sid)


def test_semantic_hash_stable_for_dict_key_order_and_masking_edge_cases():
    normalizer = _CaptureNormalizer(strict_pii_matching=False)
    left = {
        "args": [{"token_value": "abc", "email": "alpha@example.com"}],
        "kwargs": {"x": 1, "y": 2},
    }
    right = {
        "kwargs": {"y": 2, "x": 1},
        "args": [{"email": "alpha@example.com", "token_value": "abc"}],
    }

    masked_left = normalizer.mask_pii(left)
    masked_right = normalizer.mask_pii(right)

    assert masked_left["args"][0]["token_value"] == "[MASKED_SENSITIVE_KEY]"
    assert masked_right["args"][0]["token_value"] == "[MASKED_SENSITIVE_KEY]"
    assert _Fingerprint.semantic_hash(masked_left) == _Fingerprint.semantic_hash(masked_right)


def test_negative_zero_same_semantic_id_as_positive_zero():
    """
    -0.0 == 0.0 in Python and IEEE 754; semantic_id must reflect this.
    Without normalisation, str(-0.0) == '-0.0' != '0.0', producing
    different hashes for equal inputs.
    """
    from sst.core import _CaptureNormalizer, _Fingerprint

    n = _CaptureNormalizer()

    assert _Fingerprint.semantic_hash(n.serialize(-0.0)) == _Fingerprint.semantic_hash(
        n.serialize(0.0)
    )

    assert _Fingerprint.semantic_hash({"x": -0.0}) == _Fingerprint.semantic_hash({"x": 0.0})

    # type-strict invariants untouched
    assert _Fingerprint.semantic_hash(True) != _Fingerprint.semantic_hash(1)
    assert _Fingerprint.semantic_hash(1.0) != _Fingerprint.semantic_hash(1)
