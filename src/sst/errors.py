"""Structured SST error taxonomy used for deterministic, auditable failures."""

from __future__ import annotations

from dataclasses import dataclass


class SSTError(Exception):
    """Base class for all SST domain exceptions."""

    def __init__(self, error_code: str, category: str, explanation: str, actionable: bool = True):
        self.error_code = error_code
        self.category = category
        self.explanation = explanation
        self.actionable = actionable
        super().__init__(f"[{self.category}:{self.error_code}] {self.explanation}")


class CaptureContractError(SSTError):
    def __init__(self, explanation: str, actionable: bool = True):
        super().__init__("CAPTURE_CONTRACT", "CAPTURE", explanation, actionable)


class BaselineFormatError(SSTError):
    def __init__(self, explanation: str, actionable: bool = True):
        super().__init__("BASELINE_FORMAT", "BASELINE", explanation, actionable)


class BaselineValidationError(SSTError):
    def __init__(self, explanation: str, actionable: bool = True):
        super().__init__("BASELINE_VALIDATION", "BASELINE", explanation, actionable)


class ScenarioNotFoundError(SSTError):
    def __init__(self, explanation: str):
        super().__init__("SCENARIO_NOT_FOUND", "BASELINE", explanation, True)


class DiffContractError(SSTError):
    def __init__(self, explanation: str, actionable: bool = True):
        super().__init__("DIFF_CONTRACT", "DIFF", explanation, actionable)


class ReplayDeterminismError(SSTError):
    def __init__(self, explanation: str, actionable: bool = True):
        super().__init__("REPLAY_NON_DETERMINISTIC", "REPLAY", explanation, actionable)


class ReplayExecutionError(SSTError):
    def __init__(self, explanation: str, actionable: bool = True):
        super().__init__("REPLAY_EXECUTION", "REPLAY", explanation, actionable)


class GovernancePolicyError(SSTError):
    def __init__(self, explanation: str, actionable: bool = True):
        super().__init__("GOVERNANCE_POLICY", "GOVERNANCE", explanation, actionable)


@dataclass
class RegressionDetectedError(RuntimeError, SSTError):
    """Raised when verify/replay detects a semantic regression."""

    message: str
    scenario_id: str
    reason_code: str = "SEMANTIC_REGRESSION"

    def __post_init__(self) -> None:
        SSTError.__init__(self, self.reason_code, "DIFF", self.message, True)
        RuntimeError.__init__(self, self.message)

    def __str__(self) -> str:
        return self.message
