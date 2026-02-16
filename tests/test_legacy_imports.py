"""sst.sst_schema must be a transparent alias for sst.schema."""
from sst.schema import ScenarioSchema
from sst.schema import ValidationError
from sst.schema import validate_scenario_schema
from sst.sst_schema import ScenarioSchema as LegacyScenarioSchema
from sst.sst_schema import ValidationError as LegacyValidationError
from sst.sst_schema import validate_scenario_schema as legacy_validate


def test_scenario_schema_is_same_class():
    assert ScenarioSchema is LegacyScenarioSchema


def test_validation_error_is_same_class():
    assert ValidationError is LegacyValidationError


def test_validate_function_is_same_object():
    assert validate_scenario_schema is legacy_validate


def test_legacy_validate_raises_same_exception_type():
    import pytest

    with pytest.raises(ValidationError):
        legacy_validate(
            {
                "module": "m",
                "function": "f",
                "semantic_id": "abc",
                "input": [],
                "output": {},
            }
        )
