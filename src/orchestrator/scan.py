"""Планирование сканирования Git и завершение дерева задач."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from feed import Feed, LeaseLostError
from git_collector import CommitInfo, GitCollector, GitCollectorError
from models import (
    Commit,
    CommitAnalysisOutput,
    CommitAnalysisPayload,
    CommitParent,
    CommitStatus,
    ErrorCode,
    Outcome,
    ScanMode,
    ScanSummary,
    ScanTaskPayload,
    Task,
    TaskError,
    TaskKind,
    TaskResult,
    TaskStatus,
)
from models.common.clock import utc_now
from storage import Storage, StorageSession


class ScanOrchestrator:
    """Создаёт дерево задач по SHA и закрывает запуск после завершения детей."""

    def __init__(
        self,
        *,
        feed: Feed,
        storage: Storage,
        collector_for: Callable[[str], GitCollector],
        worker_id: str = "scan-planner",
        lease_s: float = 30.0,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        self.feed = feed
        self.storage = storage
        self.collector_for = collector_for
        self.worker_id = worker_id
        self.lease_s = lease_s

    async def start_scan(
        self, repository_id: int, payload: ScanTaskPayload | None = None
    ) -> Task:
        """Поставить корень либо вернуть незавершённый запуск репозитория."""
        task = Task.repository_scan(
            repository_id=repository_id,
            payload=payload if payload is not None else ScanTaskPayload(),
            nonce=secrets.token_bytes(16),
        )
        return await self.storage.run_in_transaction(
            lambda session: session.get_active_scan(repository_id)
            or session.save_task(task)
        )

    async def plan_once(self) -> bool:
        """Взять один ожидающий запуск и атомарно создать дочерние задачи."""
        task = await self.feed.claim(
            (TaskKind.REPOSITORY_SCAN,), self.worker_id, self.lease_s
        )
        if task is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(task.id))
        try:
            await self._plan(task, heartbeat)
        except LeaseLostError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, GitCollectorError) else ErrorCode.INTERNAL
            try:
                await self.feed.fail(
                    task,
                    TaskError(
                        code,
                        "scan planning failed",
                        retryable=not isinstance(exc, ValueError),
                    ),
                )
            except LeaseLostError:
                pass
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        return True

    async def finalize(self, run_id: int) -> ScanSummary | None:
        """Закрыть готовый запуск; вернуть None, пока дочерние задачи работают."""
        return await self.storage.run_in_transaction(
            lambda session: self._finalize_in_transaction(session, run_id)
        )

    async def finalize_ready(self) -> list[int]:
        """Проверить все ожидающие запуски и вернуть id завершённых."""
        waiting = await self.storage.read(lambda session: session.list_waiting_scans())
        completed: list[int] = []
        for root in waiting:
            if root.id is not None and await self.finalize(root.id) is not None:
                completed.append(root.id)
        return completed

    async def _heartbeat(self, task_id: int) -> None:
        while True:
            await asyncio.sleep(self.lease_s / 3)
            await self.feed.heartbeat(task_id)

    async def _plan(self, task: Task, heartbeat: asyncio.Task[None]) -> None:
        if task.id is None or not isinstance(task.payload, ScanTaskPayload):
            raise ValueError("invalid REPOSITORY_SCAN task")
        payload = task.payload
        if payload.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if payload.mode is ScanMode.TARGETED and not payload.target_shas:
            raise ValueError("target_shas must not be empty")
        repository = await self.storage.read(
            lambda session: session.get_repository(task.repository_id)
        )
        if repository is None:
            raise ValueError("repository not found")
        refs = payload.target_shas if payload.mode is ScanMode.TARGETED else payload.refs
        commits = await self.collector_for(repository.location).list_commits(refs)
        targets = (
            {bytes.fromhex(sha) for sha in payload.target_shas}
            if payload.mode is ScanMode.TARGETED else None
        )
        if targets is not None and not targets.issubset({info.sha for info in commits}):
            raise ValueError("target commit not found in Git history")
        if heartbeat.done():
            heartbeat.result()

        def persist(session: StorageSession) -> None:
            now = utc_now()
            root = session.get_task(task.id)
            if (
                root is None
                or root.status is not TaskStatus.RUNNING
                or root.lease_owner != task.lease_owner
                or root.lease_expires_at is None
                or root.lease_expires_at <= now
            ):
                raise LeaseLostError("scan lease is not active")
            actual_mode = payload.mode
            if actual_mode is ScanMode.INCREMENTAL and not session.has_processed_commits(
                task.repository_id
            ):
                actual_mode = ScanMode.FULL
            selected: list[int] = []
            for info in commits:
                saved = self._save_commit(session, task.repository_id, info)
                if (
                    (actual_mode is ScanMode.TARGETED or saved.status is not CommitStatus.PROCESSED)
                    and (targets is None or info.sha in targets)
                ):
                    selected.append(saved.id)
            root.payload = replace(
                payload,
                mode=actual_mode,
                planned_commits_total=len(commits),
                planned_commits_new=len(selected),
            )
            for offset in range(0, len(selected), payload.batch_size):
                child = Task.commit_analysis(
                    repository_id=task.repository_id,
                    payload=CommitAnalysisPayload(selected[offset:offset + payload.batch_size]),
                    root_task_id=task.id,
                    parent_task_id=task.id,
                )
                session.save_task(child)
            if selected:
                root.transition(TaskStatus.WAITING_CHILDREN, now)
            else:
                root.transition(TaskStatus.SUCCEEDED, now)
                session.save_task_result(
                    TaskResult(task_id=task.id, outcome=Outcome.OK, output=self._empty_summary(root))
                )
                self._mark_repository_scanned(session, repository.id, task.id, now)
            session.update_task(root)

        await self.storage.run_in_transaction(persist)

    @staticmethod
    def _save_commit(
        session: StorageSession, repository_id: int, info: CommitInfo
    ) -> Commit:
        saved = session.save_commit(
            Commit(
                repository_id=repository_id,
                sha=info.sha,
                is_merge=info.is_merge,
                committed_at=info.committed_at,
            )
        )
        for position, parent_sha in enumerate(info.parent_shas):
            session.save_commit_parent(
                CommitParent(commit_id=saved.id, position=position, parent_sha=parent_sha)
            )
        return saved

    @staticmethod
    def _empty_summary(root: Task) -> ScanSummary:
        payload = root.payload
        return ScanSummary(
            mode=payload.mode,
            commits_total=payload.planned_commits_total or 0,
            commits_new=0,
            commits_done=0,
            commits_failed=0,
            candidates=0,
            ai_tasks=0,
            ai_failed=0,
            incidents_opened=0,
            incidents_updated=0,
        )

    def _finalize_in_transaction(
        self, session: StorageSession, run_id: int
    ) -> ScanSummary | None:
        root = session.get_task(run_id)
        if root is None or root.kind is not TaskKind.REPOSITORY_SCAN:
            raise ValueError("scan root not found")
        if root.status is TaskStatus.SUCCEEDED:
            result = session.get_task_result(run_id)
            return result.output if result is not None else None
        if root.status is not TaskStatus.WAITING_CHILDREN:
            return None
        children = session.list_run_tasks(run_id)
        if any(not child.is_terminal for child in children):
            return None
        commit_outputs: list[CommitAnalysisOutput] = []
        incident_ids: set[int] = set()
        ai_failed = 0
        partial = False
        for child in children:
            result = session.get_task_result(child.id)
            if child.status is not TaskStatus.SUCCEEDED or result is None:
                partial = True
            elif result.outcome is not Outcome.OK:
                partial = True
            if child.kind is TaskKind.AI_VALIDATION:
                ai_failed += child.status is TaskStatus.FAILED
                if result is not None and result.output is not None:
                    incident_id = getattr(result.output, "incident_id", None)
                    if incident_id is not None:
                        incident_ids.add(incident_id)
            elif child.kind is TaskKind.COMMIT_ANALYSIS and result is not None:
                if isinstance(result.output, CommitAnalysisOutput):
                    commit_outputs.append(result.output)
                    incident_ids.update(result.output.incident_ids)
        opened = 0
        for incident_id in incident_ids:
            incident = session.get_incident(incident_id)
            if incident is not None and root.started_at is not None:
                opened += incident.created_at >= root.started_at
        payload = root.payload
        summary = ScanSummary(
            mode=payload.mode,
            commits_total=payload.planned_commits_total or 0,
            commits_new=payload.planned_commits_new or 0,
            commits_done=sum(len(output.commit_ids_done) for output in commit_outputs),
            commits_failed=sum(len(output.commit_ids_failed) for output in commit_outputs),
            candidates=sum(output.candidates_total for output in commit_outputs),
            ai_tasks=sum(len(output.ai_task_ids) for output in commit_outputs),
            ai_failed=ai_failed,
            incidents_opened=opened,
            incidents_updated=len(incident_ids) - opened,
        )
        partial |= summary.commits_failed > 0
        now = utc_now()
        root.transition(TaskStatus.SUCCEEDED, now)
        session.update_task(root)
        session.save_task_result(
            TaskResult(
                task_id=run_id,
                outcome=Outcome.PARTIAL if partial else Outcome.OK,
                output=summary,
            )
        )
        self._mark_repository_scanned(session, root.repository_id, run_id, now)
        return summary

    @staticmethod
    def _mark_repository_scanned(
        session: StorageSession, repository_id: int, run_id: int, now: datetime
    ) -> None:
        repository = session.get_repository(repository_id)
        if repository is None:
            raise ValueError("repository not found")
        version = repository.version
        repository.last_scan_at = now
        repository.last_scan_task_id = run_id
        session.update_repository(repository, expected_version=version)
