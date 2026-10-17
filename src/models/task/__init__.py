"""Модели ленты задач (tasks, results, payload скана).

Регистрация payload/output ``AI_VALIDATION`` — в треке ai_validation.
``TaskKind.AI_VALIDATION`` зарезервирован, чтобы числовые значения не менялись.
"""

from .commit_analysis_output import CommitAnalysisOutput
from .commit_analysis_payload import CommitAnalysisPayload
from .scan_summary import ScanSummary
from .scan_task_payload import ScanMode, ScanTaskPayload, Trigger
from .task import Task, TaskPayload
from .task_error import ErrorCode, TaskError
from .task_kinds import (
    TaskKind,
    TaskStatus,
    dump_output,
    dump_payload,
    parse_output,
    parse_payload,
    register_task_kind,
    spec_for,
)
from .task_result import Outcome, TaskOutput, TaskResult

__all__ = [
    "CommitAnalysisOutput",
    "CommitAnalysisPayload",
    "ErrorCode",
    "Outcome",
    "ScanMode",
    "ScanSummary",
    "ScanTaskPayload",
    "Task",
    "TaskError",
    "TaskKind",
    "TaskOutput",
    "TaskPayload",
    "TaskResult",
    "TaskStatus",
    "Trigger",
    "dump_output",
    "dump_payload",
    "parse_output",
    "parse_payload",
    "register_task_kind",
    "spec_for",
]
