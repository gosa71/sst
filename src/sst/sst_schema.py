"""Backward-compatible schema validation imports.

Prefer importing from ``sst.schema`` for new code.
"""

from .schema import ScenarioSchema, ValidationError, validate_scenario_schema

__all__ = ["ScenarioSchema", "ValidationError", "validate_scenario_schema"]
