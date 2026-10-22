"""Публичный контракт durable-ленты задач."""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any, Protocol

from models import Task, TaskError, TaskKind, TaskResult

SideEffects = Callable[[Any], None]


class Feed(Protocol):
    """Граница очереди, через которую worker'ы получают и завершают задачи."""

    async def enqueue(self, task: Task) -> Task:
        """Идемпотентно поставить задачу в очередь."""
        ...

    async def claim(
        self,
        kinds: Collection[TaskKind],
        worker_id: str,
        lease_s: float,
    ) -> Task | None:
        """Атомарно арендовать следующую готовую задачу подходящего вида."""
        ...

    async def heartbeat(self, task_id: int) -> None:
        """Продлить действующую аренду задачи."""
        ...

    async def complete(
        self,
        task: Task,
        result: TaskResult,
        side_effects: SideEffects | None = None,
    ) -> None:
        """Атомарно завершить задачу, результат и связанные записи."""
        ...

    async def fail(self, task: Task, error: TaskError) -> None:
        """Зафиксировать ошибку, запланировав retry либо окончательный провал."""
        ...

    async def reap_expired(self) -> int:
        """Перевыдать задачи с истёкшей арендой и вернуть их число."""
        ...

    async def cancel(self, run_id: int) -> int:
        """Отменить все нетерминальные задачи запуска и вернуть их число."""
        ...
