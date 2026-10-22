"""``Task`` — единица работы в ленте (таблица ``tasks``)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Union

from ..common.clock import utc_now
from ..exceptions import InvalidTransition
from .commit_analysis_payload import CommitAnalysisPayload
from .idempotency import (
    KEY_SIZE,
    key_for_commit_analysis,
    key_for_repository_scan,
)
from .scan_task_payload import ScanTaskPayload
from .task_error import ErrorCode, TaskError
from .task_kinds import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    TaskKind,
    TaskKindSpec,
    TaskStatus,
    spec_for,
)

TaskPayload = Union[ScanTaskPayload, CommitAnalysisPayload]

DEFAULT_RETRY_BASE = timedelta(seconds=1)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass(slots=True)
class Task:
    """Задача ленты: claim/lease/retry, статус только через ``transition``.

    Запуск анализа = корневая ``REPOSITORY_SCAN``. ``idempotency_key`` защищает от дублей.
    ``run_id = root_task_id or id`` — идентификатор запуска для дочерних задач.
    """

    kind: TaskKind
    repository_id: int
    payload: TaskPayload
    idempotency_key: bytes
    id: int | None = None
    status: TaskStatus = TaskStatus.QUEUED
    root_task_id: int | None = None
    parent_task_id: int | None = None
    priority: int | None = None
    attempts: int = 0
    max_attempts: int | None = None
    not_before: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    error_retryable: bool | None = None
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        spec = spec_for(self.kind)
        if self.priority is None:
            self.priority = spec.default_priority
        if self.max_attempts is None:
            self.max_attempts = spec.default_max_attempts
        if len(self.idempotency_key) != KEY_SIZE:
            raise ValueError(
                f"idempotency_key must be {KEY_SIZE} bytes, got {len(self.idempotency_key)}"
            )

    @property
    def spec(self) -> TaskKindSpec:
        """Спецификация вида задачи из реестра."""
        return spec_for(self.kind)

    @property
    def is_terminal(self) -> bool:
        """``True``, если задача в терминальном статусе."""
        return self.status in TERMINAL_STATUSES

    @property
    def run_id(self) -> int | None:
        """Id запуска: у корня — свой ``id``, у дочерних — ``root_task_id``."""
        return self.root_task_id or self.id

    @property
    def error(self) -> TaskError | None:
        """Последняя ошибка как value object, либо ``None``."""
        if self.error_code is None:
            return None
        return TaskError(
            code=self.error_code,
            message=self.error_message or "",
            retryable=bool(self.error_retryable),
        )

    def apply_error(self, error: TaskError) -> None:
        """Записать поля ошибки (без смены статуса)."""
        self.error_code = error.code
        self.error_message = error.message
        self.error_retryable = error.retryable

    def transition(self, new_status: TaskStatus, now: datetime) -> None:
        """Сменить статус по автомату; при ``RUNNING`` увеличить ``attempts``."""
        allowed = TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise InvalidTransition(self.status, new_status, entity="Task")
        if new_status is TaskStatus.WAITING_CHILDREN and self.kind is not TaskKind.REPOSITORY_SCAN:
            raise InvalidTransition(self.status, new_status, entity="Task")
        self.status = new_status
        if new_status is TaskStatus.RUNNING:
            self.attempts += 1
            if self.started_at is None:
                self.started_at = now
        else:
            self.lease_owner = None
            self.lease_expires_at = None
        if new_status in TERMINAL_STATUSES:
            self.finished_at = now

    def schedule_retry(
        self,
        error: TaskError,
        now: datetime | None = None,
        *,
        base: timedelta = DEFAULT_RETRY_BASE,
    ) -> None:
        """Вернуть в ``QUEUED`` с backoff либо перевести в ``FAILED``, если попытки исчерпаны."""
        when = now if now is not None else utc_now()
        self.apply_error(error)
        max_attempts = self.max_attempts if self.max_attempts is not None else 3
        if error.retryable and self.attempts < max_attempts:
            exponent = max(self.attempts, 1) - 1
            self.not_before = when + base * (2**exponent)
            self.transition(TaskStatus.QUEUED, when)
            return
        self.transition(TaskStatus.FAILED, when)

    def __repr__(self) -> str:
        key = _hash_bytes(self.idempotency_key)
        return (
            f"Task(id={self.id!r}, kind={self.kind!r}, repository_id={self.repository_id}, "
            f"status={self.status!r}, idempotency_key=sha256:{key})"
        )

    @classmethod
    def repository_scan(
        cls,
        *,
        repository_id: int,
        payload: ScanTaskPayload,
        nonce: bytes,
        **kwargs: object,
    ) -> Task:
        """Фабрика корневой задачи ``REPOSITORY_SCAN`` с корректным ``idempotency_key``."""
        return cls(
            kind=TaskKind.REPOSITORY_SCAN,
            repository_id=repository_id,
            payload=payload,
            idempotency_key=key_for_repository_scan(repository_id, nonce),
            **kwargs,  # type: ignore[arg-type]
        )

    @classmethod
    def commit_analysis(
        cls,
        *,
        repository_id: int,
        payload: CommitAnalysisPayload,
        root_task_id: int | None = None,
        **kwargs: object,
    ) -> Task:
        """Фабрика ``COMMIT_ANALYSIS`` с ключом по запуску и отсортированным id."""
        return cls(
            kind=TaskKind.COMMIT_ANALYSIS,
            repository_id=repository_id,
            payload=payload,
            root_task_id=root_task_id,
            idempotency_key=key_for_commit_analysis(
                repository_id, payload.commit_ids, root_task_id,
            ),
            **kwargs,  # type: ignore[arg-type]
        )
