"""Feed / work-unit models (tasks, results, scan payloads).

AI_VALIDATION payload/output registration is owned by the ai_validation track.
``TaskKind.AI_VALIDATION`` stays reserved so numeric values never change.
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
