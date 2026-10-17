"""Domain models. Fields and tables: ``docs/models.md``.

Layout by concern (one class — one file inside each package):

* ``common`` — clock / JSON codec helpers
* ``repository`` — Repository, Commit, CommitParent
* ``task`` — Task feed, payloads, results
* ``candidate`` — RawCandidate, Candidate, scoring VO
* ``incident`` — Incident, Occurrence, events, outbox

AI models (``AiVerdict``, AI_VALIDATION payload/output) are owned by the
ai_validation track and are not present in this package yet.
"""

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
