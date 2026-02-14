"""Centralized strict schema validation for SST baseline scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import __version__
from .errors import BaselineValidationError


class ValidationError(BaselineValidationError):
    """Raised when a baseline scenario violates the strict schema contract."""


@dataclass(frozen=True)
class ScenarioSchema:
    module: str
    function: str
    semantic_id: str
    input: dict[str, Any]
    output: Any
    engine_version: str = __version__


_REQUIRED_FIELDS: tuple[str, ...] = ("module", "function", "semantic_id", "input", "output")


def validate_scenario_schema(scenario: dict[str, Any]) -> ScenarioSchema:
    """Validate strict required fields and runtime value types for baseline scenarios."""

    if not isinstance(scenario, dict):
        raise ValidationError("Baseline scenario must be a JSON object")

    for field_name in _REQUIRED_FIELDS:
        if field_name not in scenario:
            raise ValidationError(f"Baseline scenario missing required field '{field_name}'")

    for field_name in ("module", "function", "semantic_id"):
        value = scenario[field_name]
        if not isinstance(value, str):
            actual = type(value).__name__
            raise ValidationError(
                f"Invalid baseline scenario field '{field_name}': expected str, got {actual}"
            )
        if not value.strip():
            raise ValidationError(
                f"Invalid baseline scenario field '{field_name}': must be non-empty string"
            )

    if "engine_version" in scenario:
        engine_version = scenario["engine_version"]
        if not isinstance(engine_version, str):
            actual = type(engine_version).__name__
            raise ValidationError(
                f"Invalid baseline scenario field 'engine_version': expected str, got {actual}"
            )
        if not engine_version.strip():
            raise ValidationError(
                "Invalid baseline scenario field 'engine_version': must be non-empty string"
            )

    input_value = scenario["input"]
    if not isinstance(input_value, dict):
        actual = type(input_value).__name__
        raise ValidationError(
            f"Invalid baseline scenario field 'input': expected dict, got {actual}"
        )

    if "inputs" in scenario and not isinstance(scenario["inputs"], (dict, list)):
        actual = type(scenario["inputs"]).__name__
        raise ValidationError(
            f"Invalid baseline scenario field 'inputs': expected dict or list, got {actual}"
        )

    return ScenarioSchema(
        module=scenario["module"],
        function=scenario["function"],
        semantic_id=scenario["semantic_id"],
        input=input_value,
        output=scenario["output"],
        engine_version=str(scenario.get("engine_version", __version__)),
    )
