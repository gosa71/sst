from __future__ import annotations

import os
import time
from sst.core import sst


@sst.capture
def deterministic_add(a: int, b: int) -> int:
    return a + b


@sst.capture
def nondeterministic_timestamp() -> int:
    if os.getenv("FIXED_TS") == "1":
        return 1700000000
    return int(time.time())


@sst.capture
def failing_case(x: int) -> int:
    if os.getenv("FAILING_MODE") == "regression":
        return x + 2
    return x + 1


def main() -> None:
    case = os.getenv("RUN_CASE", "all")
    if case in {"deterministic_add", "all"}:
        print("deterministic_add", deterministic_add(2, 3))
    if case in {"nondeterministic_timestamp", "all"}:
        print("nondeterministic_timestamp", nondeterministic_timestamp())
    if case in {"failing_case", "all"}:
        print("failing_case", failing_case(10))


if __name__ == "__main__":
    main()
