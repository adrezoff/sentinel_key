"""``TaskResult`` — at most one per task (table ``task_results``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Union

from ..common.clock import utc_now
from .commit_analysis_output import CommitAnalysisOutput
from .scan_summary import ScanSummary
from .task_error import ErrorCode, TaskError

# AiValidationOutput is added by the ai_validation track when that kind is registered.
TaskOutput = Union[ScanSummary, CommitAnalysisOutput]


class Outcome(IntEnum):
    OK = 1
    PARTIAL = 2
    FAILED = 3


@dataclass(slots=True)
class TaskResult:
    task_id: int
    outcome: Outcome
    created_at: datetime = field(default_factory=utc_now)
    output: TaskOutput | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    error_retryable: bool | None = None
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def error(self) -> TaskError | None:
        if self.error_code is None:
            return None
        return TaskError(
            code=self.error_code,
            message=self.error_message or "",
            retryable=bool(self.error_retryable),
        )

    def apply_error(self, error: TaskError) -> None:
        self.error_code = error.code
        self.error_message = error.message
        self.error_retryable = error.retryable
