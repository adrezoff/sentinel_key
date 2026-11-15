# -*- coding: utf-8 -*-
"""Реализация фабрики AI-задач для CommitWorker."""

from __future__ import annotations

from ai_validation.llm_client import LlmConfig
from models import (
    AiValidationPayload,
    Candidate,
    Task,
    TaskKind,
)
from models.task.idempotency import key_for_ai_validation
from workers.commit import AiTaskFactory


class DefaultAiTaskFactory:
    """Стандартная фабрика задач AI_VALIDATION.

    Создаёт задачу с правильным idempotency_key (candidate_id + prompt_version)
    и payload'ом AiValidationPayload.
    """

    def __init__(self, llm_config: LlmConfig | None = None) -> None:
        self._llm_config = llm_config or LlmConfig()

    def __call__(
        self,
        candidate: Candidate,
        *,
        root_task_id: int,
        parent_task_id: int,
    ) -> Task:
        """Создать задачу AI_VALIDATION для кандидата.

        Args:
            candidate: Сохранённый кандидат с route=AI_VALIDATION.
            root_task_id: ID корневой задачи REPOSITORY_SCAN.
            parent_task_id: ID задачи COMMIT_ANALYSIS.

        Returns:
            Task с kind=AI_VALIDATION и правильным idempotency_key.
        """
        if candidate.id is None:
            raise ValueError("candidate.id is required")
        if candidate.route.name != "AI_VALIDATION":
            raise ValueError("candidate must have route=AI_VALIDATION")

        payload = AiValidationPayload(
            candidate_id=candidate.id,
            prompt_version=self._llm_config.prompt_version,
            model=None,  # используем модель из конфига LLM
        )

        return Task.ai_validation(
            repository_id=candidate.repository_id,
            payload=payload,
            root_task_id=root_task_id,
            parent_task_id=parent_task_id,
            # idempotency_key генерируется внутри Task.ai_validation
            # через key_for_ai_validation(candidate_id, prompt_version)
        )