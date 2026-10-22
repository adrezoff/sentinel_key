"""Последовательная обработка пачки коммитов и атомарная запись результата."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from feed import Feed, LeaseLostError
from git_collector import DiffLineKind, GitCollector, GitCollectorError
from incident_manager import IncidentManager
from models import (
    Candidate,
    Commit,
    CommitAnalysisOutput,
    CommitAnalysisPayload,
    ErrorCode,
    Outcome,
    Route,
    ScanTaskPayload,
    Task,
    TaskError,
    TaskKind,
    TaskResult,
)
from models.common.clock import utc_now
from preprocessor import Preprocessor
from static_analyzer import StaticAnalyzer
from storage import Storage, StorageSession

from .base import WorkerSettings


class AiTaskFactory(Protocol):
    """Будущая интеграция: создать задачу AI для уже сохранённого кандидата."""

    def __call__(self, candidate: Candidate, *, root_task_id: int, parent_task_id: int) -> Task:
        """Вернуть задачу с версионированным ключом идемпотентности."""
        ...


@dataclass(slots=True)
class _CommitWork:
    """Промежуточный результат без сырых значений секрета."""

    commit: Commit
    candidates: list[Candidate] = field(default_factory=list)
    lines_analyzed: int = 0
    error_code: ErrorCode | None = None


class CommitWorker:
    """Берёт COMMIT_ANALYSIS из ленты и фиксирует всю пачку одной транзакцией."""

    def __init__(
        self,
        *,
        feed: Feed,
        storage: Storage,
        collector_for: Callable[[str], GitCollector],
        preprocessor: Preprocessor,
        analyzer: StaticAnalyzer,
        incident_manager: IncidentManager,
        settings: WorkerSettings,
        ai_task_factory: AiTaskFactory | None = None,
    ) -> None:
        if settings.kinds != (TaskKind.COMMIT_ANALYSIS,):
            raise ValueError("CommitWorker requires COMMIT_ANALYSIS settings")
        self.feed = feed
        self.storage = storage
        self.collector_for = collector_for
        self.preprocessor = preprocessor
        self.analyzer = analyzer
        self.incident_manager = incident_manager
        self.settings = settings
        self.ai_task_factory = ai_task_factory

    async def run_once(self) -> bool:
        """Обработать одну доступную задачу; вернуть False при пустой ленте."""
        task = await self.feed.claim(
            self.settings.kinds, self.settings.worker_id, self.settings.lease_s
        )
        if task is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(task.id))
        try:
            await self._process(task, heartbeat)
            if heartbeat.done():
                heartbeat.result()
        except LeaseLostError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await self.feed.fail(
                    task,
                    TaskError(ErrorCode.INTERNAL, "commit analysis failed", retryable=True),
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

    async def run(self, stop: asyncio.Event) -> None:
        """Опросить ленту до сигнала остановки, завершив текущую задачу."""
        while not stop.is_set():
            if await self.run_once():
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.poll_interval_s)
            except asyncio.TimeoutError:
                pass

    async def _heartbeat(self, task_id: int) -> None:
        interval = self.settings.lease_s / 3
        while True:
            await asyncio.sleep(interval)
            await self.feed.heartbeat(task_id)

    async def _process(self, task: Task, heartbeat: asyncio.Task[None]) -> None:
        if task.id is None or not isinstance(task.payload, CommitAnalysisPayload):
            raise ValueError("invalid COMMIT_ANALYSIS task")
        repository = await self.storage.read(
            lambda session: session.get_repository(task.repository_id)
        )
        if repository is None:
            raise ValueError("repository not found")
        root = None
        if task.root_task_id is not None:
            root = await self.storage.read(
                lambda session: session.get_task(task.root_task_id)
            )
            if root is None or not isinstance(root.payload, ScanTaskPayload):
                raise ValueError("scan root not found")
        ai_enabled = bool(root and root.payload.ai_enabled and self.ai_task_factory)
        collector = self.collector_for(repository.location)
        work: list[_CommitWork] = []
        for commit_id in task.payload.commit_ids:
            commit = await self.storage.read(
                lambda session, commit_id=commit_id: session.get_commit(commit_id)
            )
            if commit is None or commit.repository_id != task.repository_id:
                raise ValueError("commit does not belong to repository")
            item = _CommitWork(commit)
            work.append(item)
            try:
                async for file_diff in collector.get_diff(commit.sha):
                    try:
                        file_lines = sum(
                            line.kind is DiffLineKind.ADD
                            for hunk in file_diff.hunks
                            for line in hunk.lines
                        )
                        file_candidates: list[Candidate] = []
                        for raw in self.preprocessor.process(
                            file_diff, repository_id=task.repository_id, commit_id=commit_id
                        ):
                            candidate = await self.analyzer.analyze_async(
                                raw, ai_enabled=ai_enabled
                            )
                            if candidate.route is not Route.IGNORE:
                                file_candidates.append(candidate)
                        item.candidates.extend(file_candidates)
                        item.lines_analyzed += file_lines
                    except Exception:
                        # Ошибка одного файла не прерывает остальные файлы коммита.
                        continue
            except GitCollectorError as exc:
                item.error_code = exc.code
                item.candidates.clear()
            except Exception:
                item.error_code = ErrorCode.GIT_ERROR
                item.candidates.clear()

        result = TaskResult(
            task_id=task.id,
            outcome=Outcome.PARTIAL if any(item.error_code for item in work) else Outcome.OK,
        )

        def persist(session: StorageSession) -> None:
            done: list[int] = []
            failed: list[int] = []
            ai_task_ids: list[int] = []
            incident_ids: list[int] = []
            lines_analyzed = 0
            candidates_total = 0
            suspicious = 0
            for item in work:
                commit = item.commit
                if commit.id is None:
                    raise ValueError("commit.id is required")
                if item.error_code is not None:
                    commit.mark_failed(
                        error_code=item.error_code,
                        task_id=task.id,
                        scan_run_id=task.run_id,
                    )
                    failed.append(commit.id)
                else:
                    for candidate in item.candidates:
                        saved = session.save_candidate(candidate)
                        candidates_total += 1
                        if saved.route is Route.SUSPICIOUS:
                            suspicious += 1
                        if (
                            ai_enabled
                            and saved.route is Route.AI_VALIDATION
                            and self.ai_task_factory is not None
                        ):
                            ai_task = self.ai_task_factory(
                                saved, root_task_id=task.run_id, parent_task_id=task.id
                            )
                            if ai_task.kind is not TaskKind.AI_VALIDATION:
                                raise ValueError("AI factory returned wrong task kind")
                            queued = session.save_task(ai_task)
                            if queued.id is None:
                                raise ValueError("AI task id is required after save")
                            ai_task_ids.append(queued.id)
                        incident = self.incident_manager.process(session, saved)
                        if incident is not None and incident.id not in incident_ids:
                            incident_ids.append(incident.id)
                    commit.mark_processed(
                        now=utc_now(), task_id=task.id, scan_run_id=task.run_id
                    )
                    done.append(commit.id)
                    lines_analyzed += item.lines_analyzed
                session.update_commit(commit)
            result.output = CommitAnalysisOutput(
                done, failed, lines_analyzed, candidates_total,
                suspicious, ai_task_ids, incident_ids,
            )

        if heartbeat.done():
            heartbeat.result()
        await self.feed.complete(task, result, persist)
