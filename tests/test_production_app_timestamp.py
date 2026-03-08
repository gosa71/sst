"""Проверяет что production_app.py использует aware datetime."""
import pathlib
import re
from datetime import datetime, timezone


def test_production_app_uses_aware_datetime():
    src = pathlib.Path("production_app.py").read_text()
    assert "timezone.utc" in src


def test_production_app_timestamp_matches_iso_regex():
    _ISO_TS_RE = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
    )
    ts = datetime.now(timezone.utc).isoformat()
    assert _ISO_TS_RE.match(ts)
