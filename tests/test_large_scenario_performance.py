import time

from sst.diff import apply_diff_policy, build_structured_diff, normalize_for_compare


def _large_payload(width: int, depth: int):
    node = {"leaf": list(range(width))}
    for d in range(depth):
        node = {
            "level": d,
            "meta": {"values": [f" item-{i} " for i in range(width)]},
            "child": node,
        }
    return node


def test_large_nested_scenario_diff_under_threshold():
    baseline = _large_payload(width=120, depth=14)
    current = _large_payload(width=120, depth=14)
    current["child"]["meta"]["values"][3] = "item-CHANGED"

    started = time.perf_counter()
    normalized_baseline = normalize_for_compare(apply_diff_policy(baseline))
    normalized_current = normalize_for_compare(apply_diff_policy(current))
    changes = build_structured_diff(normalized_baseline, normalized_current)
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0
    assert changes
    assert any(change["path"].startswith("$.child") for change in changes)
