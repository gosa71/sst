"""Core transforms must be idempotent: f(f(x)) == f(x)."""
import pytest
from sst.diff import apply_diff_policy, normalize_for_compare
from sst.core import _CaptureNormalizer


PAYLOADS = [
    pytest.param(
        {"ts": "2024-01-15T10:30:00Z", "value": 42, "id": "skip"},
        id="timestamp_and_id",
    ),
    pytest.param(
        {"nested": {"items": [" b ", " a "], "count": 2}, "ratio": 3.14159265},
        id="nested_whitespace_and_float",
    ),
    pytest.param(
        {"uuid": "550e8400-e29b-41d4-a716-446655440000", "status": "ok"},
        id="uuid_like_string",
    ),
    pytest.param(
        {"z": 3, "a": 1, "m": [{"z": 2}, {"a": 1}]},
        id="unordered_keys_and_list",
    ),
    pytest.param(
        {},
        id="empty_dict",
    ),
    pytest.param(
        {"value": None, "flag": True, "count": 0},
        id="null_bool_zero",
    ),
    pytest.param(
        {"deep": {"a": {"b": {"c": {"d": "leaf"}}}}},
        id="deeply_nested",
    ),
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_normalize_after_apply_is_idempotent(payload):
    def transform(x):
        return normalize_for_compare(apply_diff_policy(x))

    first = transform(payload)
    second = transform(first)
    assert first == second, f"Not idempotent:\nfirst={first}\nsecond={second}"


@pytest.mark.parametrize("payload", PAYLOADS)
def test_apply_diff_policy_is_idempotent(payload):
    first = apply_diff_policy(payload)
    second = apply_diff_policy(first)
    assert first == second


@pytest.mark.parametrize("payload", PAYLOADS)
def test_mask_pii_is_idempotent(payload):
    n = _CaptureNormalizer()
    first = n.mask_pii(payload)
    second = n.mask_pii(first)
    assert first == second
