"""``TaskResult`` — не более одного результата на задачу (таблица ``task_results``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Union

from ..common.clock import utc_now
from .commit_analysis_output import CommitAnalysisOutput
from .scan_summary import ScanSummary
from .task_error import ErrorCode, TaskError

# AiValidationOutput появится в треке ai_validation вместе с регистрацией kind.
TaskOutput = Union[ScanSummary, CommitAnalysisOutput]


class Outcome(IntEnum):
    """Исход выполнения: полный успех, частичный (напр. LLM недоступен) или провал."""

    OK = 1
    PARTIAL = 2
    FAILED = 3


@dataclass(slots=True)
class TaskResult:
    """Итог задачи: outcome, типизированный output и опциональная ошибка/метрики."""

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
        """Ошибка результата как value object, либо ``None``."""
        if self.error_code is None:
            return None
        return TaskError(
            code=self.error_code,
            message=self.error_message or "",
            retryable=bool(self.error_retryable),
        )

    def apply_error(self, error: TaskError) -> None:
        """Записать поля ошибки в результат."""
        self.error_code = error.code
        self.error_message = error.message
        self.error_retryable = error.retryable
