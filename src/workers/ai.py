# -*- coding: utf-8 -*-
"""AI Worker: обработка задач AI_VALIDATION из ленты."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Callable

from ai_validation.llm_client import (
    LlmClient,
    LlmConfig,
    LlmError,
    LlmInvalidResponseError,
    LlmTimeoutError,
    LlmRateLimitError,
    LlmAuthError,
    LlmNetworkError,
    FakeLlmClient,
    RealLlmClient,
    build_prompt,
)
from ai_validation.masking_guard import (
    assert_safe_for_llm as masking_assert_safe,
    MaskingViolationError,
)
from feed import Feed, LeaseLostError
from incident_manager import IncidentManager
from models import (
    AiValidationOutput,
    AiValidationPayload,
    Candidate,
    ErrorCode,
    Outcome,
    Task,
    TaskError,
    TaskKind,
    TaskResult,
    TaskStatus,
)
from models.common.clock import utc_now
from storage import Storage, StorageSession
from workers.base import WorkerSettings, TaskHandler


class AiWorker:
    """Worker для обработки задач AI_VALIDATION.

    Цикл: claim → heartbeat → masking guard → prompt build → LLM call
    → parse verdict → save verdict + incident (если нужно) → complete/fail.
    """

    def __init__(
        self,
        *,
        feed: Feed,
        storage: Storage,
        incident_manager: IncidentManager,
        llm_client: LlmClient,
        settings: WorkerSettings,
    ) -> None:
        if settings.kinds != (TaskKind.AI_VALIDATION,):
            raise ValueError("AiWorker requires AI_VALIDATION settings")
        self.feed = feed
        self.storage = storage
        self.incident_manager = incident_manager
        self.llm_client = llm_client
        self.settings = settings

    async def run_once(self) -> bool:
        """Обработать одну доступную задачу AI_VALIDATION."""
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
        except MaskingViolationError as e:
            # Masking violation — не retryable, сохраняем безопасное сообщение
            try:
                await self.feed.fail(
                    task,
                    TaskError(
                        ErrorCode.MASKING_VIOLATION,
                        f"masking violation: {e.pattern_name or 'UNKNOWN'}",
                        retryable=False,
                    ),
                )
            except LeaseLostError:
                pass
        except LlmError as e:
            # LLM ошибки — маппим на соответствующие ErrorCode
            error_code = self._map_llm_error(e)
            retryable = e.retryable
            try:
                await self.feed.fail(
                    task,
                    TaskError(error_code, f"llm error: {e.code or 'UNKNOWN'}", retryable=retryable),
                )
            except LeaseLostError:
                pass
        except Exception as e:
            # Остальные ошибки — INTERNAL, retryable
            try:
                await self.feed.fail(
                    task,
                    TaskError(ErrorCode.INTERNAL, f"ai validation failed: {type(e).__name__}", retryable=True),
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

    def _map_llm_error(self, e: LlmError) -> ErrorCode:
        """Маппинг LLM исключений на ErrorCode."""
        if isinstance(e, LlmTimeoutError):
            return ErrorCode.LLM_FAILED
        if isinstance(e, LlmInvalidResponseError):
            return ErrorCode.LLM_INVALID_RESPONSE
        if isinstance(e, LlmRateLimitError):
            return ErrorCode.LLM_FAILED
        if isinstance(e, LlmAuthError):
            return ErrorCode.LLM_FAILED
        if isinstance(e, LlmNetworkError):
            return ErrorCode.LLM_FAILED
        return ErrorCode.LLM_FAILED

    async def _heartbeat(self, task_id: int) -> None:
        """Обновлять аренду задачи пока она выполняется."""
        try:
            while True:
                await asyncio.sleep(self.settings.lease_s / 3)
                await self.feed.heartbeat(task_id)
        except asyncio.CancelledError:
            pass

    async def _process(self, task: Task, heartbeat: asyncio.Task) -> None:
        """Обработать одну задачу AI_VALIDATION."""
        # Распаковка payload
        if not isinstance(task.payload, AiValidationPayload):
            raise ValueError(f"Expected AiValidationPayload, got {type(task.payload)}")
        payload: AiValidationPayload = task.payload

        # Загрузка кандидата
        candidate = await self.storage.read(
            lambda session: session.get_candidate(payload.candidate_id)
        )
        if candidate is None:
            raise ValueError(f"Candidate {payload.candidate_id} not found")

        # 1. Masking guard — проверка перед отправкой в LLM
        system_prompt, user_prompt = build_prompt(candidate)
        masking_assert_safe(system_prompt + "\n" + user_prompt)

        # 2. Вызов LLM с ретраями (внутри llm_client уже есть retry логика)
        verdict = await self.llm_client.complete(candidate, payload)

        # 3. Подготовить verdict с правильным task_id и candidate_id (AiVerdict frozen — replace)
        verdict = replace(verdict, task_id=task.id, candidate_id=payload.candidate_id)

        # 4. Атомарное завершение через storage.ai_validation_done
        # incident_changes: вызывает incident_manager.process
        # result_factory: строит TaskResult с реальным saved_verdict.id
        def incident_changes(session: StorageSession, saved_verdict) -> int | None:
            incident = self.incident_manager.process(session, candidate, verdict=saved_verdict)
            return incident.id if incident is not None else None

        def result_factory(saved_verdict, incident_id: int | None) -> TaskResult:
            return TaskResult(
                task_id=task.id,
                outcome=Outcome.OK,
                output=AiValidationOutput(
                    verdict_id=saved_verdict.id,
                    incident_id=incident_id,
                ),
            )

        await self.storage.ai_validation_done(task, verdict, incident_changes, result_factory)


def create_ai_worker(
    *,
    feed: Feed,
    storage: Storage,
    incident_manager: IncidentManager,
    llm_config: LlmConfig | None = None,
    worker_id: str = "ai-worker",
    lease_s: float = 30.0,
    poll_interval_s: float = 1.0,
    use_fake_llm: bool = False,
) -> AiWorker:
    """Фабрика для создания AiWorker с нужными зависимостями."""
    if llm_config is None:
        llm_config = LlmConfig()

    if use_fake_llm:
        llm_client: LlmClient = FakeLlmClient()
    else:
        llm_client = RealLlmClient(llm_config)

    settings = WorkerSettings(
        worker_id=worker_id,
        kinds=(TaskKind.AI_VALIDATION,),
        lease_s=lease_s,
        poll_interval_s=poll_interval_s,
    )

    return AiWorker(
        feed=feed,
        storage=storage,
        incident_manager=incident_manager,
        llm_client=llm_client,
        settings=settings,
    )