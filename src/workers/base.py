"""Минимальные типы для будущего общего цикла worker'а."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from models import Task, TaskKind, TaskResult


class TaskHandler(Protocol):
    """Обработчик одной уже арендованной задачи без управления её статусом."""

    async def __call__(self, task: Task) -> TaskResult:
        """Выполнить предметную операцию и вернуть её результат."""
        ...


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Общие параметры claim/poll для будущего цикла worker'а."""

    worker_id: str
    kinds: tuple[TaskKind, ...]
    lease_s: float = 30.0
    poll_interval_s: float = 1.0

    def __post_init__(self) -> None:
        if not self.worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if not self.kinds:
            raise ValueError("kinds must not be empty")
        if self.lease_s <= 0:
            raise ValueError("lease_s must be positive")
        if self.poll_interval_s < 0:
            raise ValueError("poll_interval_s must not be negative")
