# -*- coding: utf-8 -*-
"""AI Worker: обработка задач AI_VALIDATION из ленты."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

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
from ai_validation.masking_guard import assert_safe_for_llm as masking_assert_safe
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
        except Exception as e:
            try:
                await self.feed.fail(
                    task,
                    TaskError(ErrorCode.INTERNAL, f"ai validation failed: {e}", retryable=True),
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

        # 3. Сохранение вердикта + инцидент (в одной транзакции)
        await self.storage.run_in_transaction(
            lambda session: self._persist_verdict_and_incident(
                session, task, candidate, verdict, payload.prompt_version
            )
        )

        # 4. Завершение задачи успешно
        await self.feed.complete(
            task,
            TaskResult(
                task_id=task.id,
                outcome=Outcome.OK,
                output=AiValidationOutput(
                    verdict_id=verdict.id,
                    incident_id=None,  # будет заполнен в _persist_verdict_and_incident
                ),
            ),
            lambda session: None,  # уже сохранено в транзакции выше
        )

    def _persist_verdict_and_incident(
        self,
        session: StorageSession,
        task: Task,
        candidate: Candidate,
        verdict: "AiVerdict",
        prompt_version: int,
    ) -> int | None:
        """Сохранить вердикт и обработать инцидент (если is_secret).

        Returns:
            incident_id если инцидент создан/обновлен, иначе None.
        """
        # Сохраняем вердикт
        verdict.task_id = task.id
        saved_verdict = session.save_ai_verdict(verdict)

        # Обрабатываем инцидент через менеджер
        incident = self.incident_manager.process(
            session, candidate, verdict=saved_verdict
        )

        return incident.id if incident is not None else None


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