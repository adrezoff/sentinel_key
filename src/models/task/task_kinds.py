"""Реестр видов задач: типы payload/output и значения по умолчанию.

Новый kind: значение в ``TaskKind``, классы payload/output, затем ``register_task_kind(...)``.
Workers и storage смотрят сюда.

``TaskKind.AI_VALIDATION`` зарезервирован (значение 3). Регистрация — в треке ai_validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from ..common.codec import from_jsonable, to_jsonable
from .commit_analysis_output import CommitAnalysisOutput
from .commit_analysis_payload import CommitAnalysisPayload
from .scan_summary import ScanSummary
from .scan_task_payload import ScanTaskPayload


class TaskKind(IntEnum):
    """Вид задачи в ленте. Числовые значения закреплены."""

    REPOSITORY_SCAN = 1
    COMMIT_ANALYSIS = 2
    AI_VALIDATION = 3  # зарезервировано; регистрация в треке ai_validation


class TaskStatus(IntEnum):
    """Статус задачи. Терминальные: SUCCEEDED, FAILED, CANCELLED."""

    QUEUED = 1
    RUNNING = 2
    WAITING_CHILDREN = 3
    SUCCEEDED = 4
    FAILED = 5
    CANCELLED = 6

    @property
    def is_terminal(self) -> bool:
        """``True``, если статус конечный и задача больше не берётся в работу."""
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

# status → допустимые следующие статусы. WAITING_CHILDREN дополнительно
# ограничен kind=REPOSITORY_SCAN в ``Task.transition``.
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.WAITING_CHILDREN,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_CHILDREN: frozenset(
        {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
}


@dataclass(frozen=True, slots=True)
class TaskKindSpec:
    """Спецификация вида задачи: типы payload/output, приоритет и лимит попыток."""

    kind: TaskKind
    payload_type: type
    output_type: type | None
    default_priority: int
    default_max_attempts: int


_SPECS: dict[TaskKind, TaskKindSpec] = {}


def register_task_kind(spec: TaskKindSpec) -> TaskKindSpec:
    """Зарегистрировать вид задачи. Повторная регистрация того же kind — ошибка."""
    if spec.kind in _SPECS:
        raise ValueError(f"task kind already registered: {spec.kind!r}")
    _SPECS[spec.kind] = spec
    return spec


def spec_for(kind: TaskKind) -> TaskKindSpec:
    """Вернуть спецификацию зарегистрированного kind."""
    try:
        return _SPECS[kind]
    except KeyError as exc:
        raise KeyError(f"unknown task kind: {kind!r}") from exc


def parse_payload(kind: TaskKind, data: dict[str, Any]) -> Any:
    """Разобрать payload задачи из JSON-dict по типу из реестра."""
    return from_jsonable(spec_for(kind).payload_type, data)


def dump_payload(payload: Any) -> dict[str, Any]:
    """Сериализовать payload в JSON-dict."""
    return to_jsonable(payload)


def parse_output(kind: TaskKind, data: dict[str, Any] | None) -> Any:
    """Разобрать output задачи; ``None`` остаётся ``None``."""
    if data is None:
        return None
    output_type = spec_for(kind).output_type
    if output_type is None:
        return data
    return from_jsonable(output_type, data)


def dump_output(output: Any) -> dict[str, Any] | None:
    """Сериализовать output; ``None`` → ``None``."""
    if output is None:
        return None
    return to_jsonable(output)


register_task_kind(
    TaskKindSpec(
        kind=TaskKind.REPOSITORY_SCAN,
        payload_type=ScanTaskPayload,
        output_type=ScanSummary,
        default_priority=10,
        default_max_attempts=3,
    )
)
register_task_kind(
    TaskKindSpec(
        kind=TaskKind.COMMIT_ANALYSIS,
        payload_type=CommitAnalysisPayload,
        output_type=CommitAnalysisOutput,
        default_priority=100,
        default_max_attempts=3,
    )
)
