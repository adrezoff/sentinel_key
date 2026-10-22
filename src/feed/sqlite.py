"""Durable-лента задач поверх транзакционного SQLite-хранилища."""

from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from typing import Any

from models import ErrorCode, Outcome, Task, TaskError, TaskKind, TaskResult, TaskStatus
from models.common.clock import utc_now
from storage import Storage

from .exceptions import LeaseLostError
from .protocol import SideEffects


class SqliteFeed:
    """Реализация claim/lease/retry без собственной бизнес-логики worker'ов."""

    def __init__(
        self,
        storage: Storage,
        *,
        heartbeat_lease_s: float = 30.0,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if heartbeat_lease_s <= 0:
            raise ValueError("heartbeat_lease_s must be positive")
        self._storage = storage
        self._heartbeat_lease = timedelta(seconds=heartbeat_lease_s)
        self._clock = clock

    async def enqueue(self, task: Task) -> Task:
        """Идемпотентно поставить задачу по её ``idempotency_key``."""
        return await self._storage.run_in_transaction(
            lambda session: session.save_task(task)
        )

    async def claim(
        self,
        kinds: Collection[TaskKind],
        worker_id: str,
        lease_s: float,
    ) -> Task | None:
        """Атомарно взять первую доступную задачу в порядке priority/id."""
        normalized_kinds = tuple(dict.fromkeys(kinds))
        if not normalized_kinds:
            raise ValueError("kinds must not be empty")
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        now = self._now()
        lease_expires_at = now + timedelta(seconds=lease_s)

        def claim_one(session: Any) -> Task | None:
            task = session.find_next_queued_task(normalized_kinds, now)
            if task is None:
                return None
            task.transition(TaskStatus.RUNNING, now)
            task.lease_owner = worker_id
            task.lease_expires_at = lease_expires_at
            session.update_task(task)
            return task

        return await self._storage.run_in_transaction(claim_one)

    async def heartbeat(self, task_id: int) -> None:
        """Продлить аренду; истёкшую или завершённую задачу оживлять нельзя."""
        now = self._now()

        def extend(session: Any) -> None:
            task = session.get_task(task_id)
            self._ensure_active_lease(task, now)
            task.lease_expires_at = now + self._heartbeat_lease
            session.update_task(task)

        await self._storage.run_in_transaction(extend)

    async def complete(
        self,
        task: Task,
        result: TaskResult,
        side_effects: SideEffects | None = None,
    ) -> None:
        """Зафиксировать успешный статус, результат и побочные записи атомарно."""
        if task.id is None:
            raise ValueError("task.id is required")
        if result.task_id != task.id:
            raise ValueError("result.task_id must match task.id")
        now = self._now()

        def finish(session: Any) -> None:
            stored = session.get_task(task.id)
            self._ensure_active_lease(stored, now)
            stored.transition(TaskStatus.SUCCEEDED, now)
            session.update_task(stored)
            if side_effects is not None:
                side_effects(session)
            session.save_task_result(result)

        await self._storage.run_in_transaction(finish)

    async def fail(self, task: Task, error: TaskError) -> None:
        """Запланировать повтор или завершить задачу и сохранить итоговую ошибку."""
        if task.id is None:
            raise ValueError("task.id is required")
        now = self._now()

        def record_failure(session: Any) -> None:
            stored = session.get_task(task.id)
            self._ensure_active_lease(stored, now)
            stored.schedule_retry(error, now)
            session.update_task(stored)
            if stored.status is TaskStatus.FAILED:
                session.save_task_result(
                    TaskResult(
                        task_id=stored.id,
                        outcome=Outcome.FAILED,
                        error_code=error.code,
                        error_message=error.message,
                        error_retryable=error.retryable,
                        created_at=now,
                    )
                )

        await self._storage.run_in_transaction(record_failure)

    async def reap_expired(self) -> int:
        """Вернуть протухшие аренды в очередь с обычной retry-политикой модели."""
        now = self._now()
        error = TaskError(
            code=ErrorCode.LEASE_EXPIRED,
            message="task lease expired",
            retryable=True,
        )

        def reap(session: Any) -> int:
            tasks = session.list_expired_tasks(now)
            for task in tasks:
                task.schedule_retry(error, now)
                session.update_task(task)
                if task.status is TaskStatus.FAILED:
                    session.save_task_result(
                        TaskResult(
                            task_id=task.id,
                            outcome=Outcome.FAILED,
                            error_code=error.code,
                            error_message=error.message,
                            error_retryable=True,
                            created_at=now,
                        )
                    )
            return len(tasks)

        return await self._storage.run_in_transaction(reap)

    async def cancel(self, run_id: int) -> int:
        """Отменить корень и дочерние задачи запуска одной транзакцией."""
        now = self._now()

        def cancel_run(session: Any) -> int:
            tasks = session.list_active_run_tasks(run_id)
            for task in tasks:
                task.transition(TaskStatus.CANCELLED, now)
                session.update_task(task)
            return len(tasks)

        return await self._storage.run_in_transaction(cancel_run)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now

    @staticmethod
    def _ensure_active_lease(task: Task | None, now: datetime) -> None:
        if task is None:
            raise KeyError("task not found")
        if (
            task.status is not TaskStatus.RUNNING
            or task.lease_owner is None
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
        ):
            raise LeaseLostError("task lease is not active")

