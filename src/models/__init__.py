"""Доменные модели. Поля и таблицы: ``docs/models.md``.

Разбиение по зонам ответственности (один класс — один файл внутри пакета):

* ``common`` — часы / JSON-codec
* ``repository`` — Repository, Commit, CommitParent
* ``task`` — лента задач, payload, результаты
* ``candidate`` — RawCandidate, Candidate, scoring VO
* ``incident`` — Incident, Occurrence, события, outbox
* ``ai_validation`` — AiVerdict, AiValidationPayload, AiValidationOutput
"""

from .ai_validation import AiValidationOutput, AiValidationPayload, AiVerdict
from .candidate import Candidate, RawCandidate, Route, ScoreBreakdown
from .enums import DetectorFlags, SecretType, Severity
from .exceptions import InvalidTransition
from .incident import (
    Incident,
    IncidentEvent,
    IncidentEventKind,
    IncidentStatus,
    NotifyChannel,
    Occurrence,
    OutboxMessage,
    OutboxStatus,
)
from .repository import Commit, CommitParent, CommitStatus, Repository
from .task import (
    CommitAnalysisOutput,
    CommitAnalysisPayload,
    ErrorCode,
    Outcome,
    ScanMode,
    ScanSummary,
    ScanTaskPayload,
    Task,
    TaskError,
    TaskKind,
    TaskOutput,
    TaskPayload,
    TaskResult,
    TaskStatus,
    Trigger,
    dump_output,
    dump_payload,
    parse_output,
    parse_payload,
    register_task_kind,
    spec_for,
)

__all__ = [
    "AiValidationOutput",
    "AiValidationPayload",
    "AiVerdict",
    "Candidate",
    "Commit",
    "CommitAnalysisOutput",
    "CommitAnalysisPayload",
    "CommitParent",
    "CommitStatus",
    "DetectorFlags",
    "dump_output",
    "dump_payload",
    "ErrorCode",
    "Incident",
    "IncidentEvent",
    "IncidentEventKind",
    "IncidentStatus",
    "InvalidTransition",
    "NotifyChannel",
    "Occurrence",
    "Outcome",
    "OutboxMessage",
    "OutboxStatus",
    "parse_output",
    "parse_payload",
    "RawCandidate",
    "register_task_kind",
    "Repository",
    "Route",
    "ScanMode",
    "ScanSummary",
    "ScanTaskPayload",
    "ScoreBreakdown",
    "SecretType",
    "Severity",
    "spec_for",
    "Task",
    "TaskError",
    "TaskKind",
    "TaskOutput",
    "TaskPayload",
    "TaskResult",
    "TaskStatus",
    "Trigger",
]
