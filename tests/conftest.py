import os
import random

import pytest


@pytest.fixture(autouse=True)
def deterministic_seed():
    random.seed(1337)


@pytest.fixture(autouse=True)
def clear_sst_env(monkeypatch):
    for key in [
        "SST_ENABLED",
        "SST_VERIFY",
        "SST_CAPTURE_ENABLED",
        "SST_STORAGE_DIR",
        "SST_BASELINE_DIR",
        "SST_SAMPLING_RATE",
        "SST_REPLAY_SEED",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    os.environ.setdefault("PYTHONHASHSEED", "0")
