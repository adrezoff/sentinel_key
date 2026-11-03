"""``AiVerdict`` — вердикт ИИ по кандидату (таблица ``ai_verdicts``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..common.clock import utc_now
from ..enums import SecretType, Severity


@dataclass(frozen=True, slots=True)
class AiVerdict:
    """Финальный вердикт ИИ по кандидату после ``LLM``-валидации.

    Хранит только маскированные данные и категорию суждения:
    ``is_secret``, ``secret_type``, ``confidence``, ``severity``,
    ``reason``, ``recommendation``, ``model``, ``prompt_version``.
    Сырого значения секрета нет — ``candidate_id`` ссылается на ``Candidate``
    с ``value_masked`` / ``value_fingerprint``.
    """

    task_id: int
    candidate_id: int
    is_secret: bool
    secret_type: SecretType
    confidence: float
    severity: Severity
    reason: str
    recommendation: str
    model: str
    prompt_version: int
    id: int | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        """Валидация при создании: диапазон, enum-значения, непустые строки."""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )
        if self.secret_type not in list(SecretType):
            raise ValueError(f"secret_type must be one of SecretType enum")
        if self.severity not in list(Severity):
            raise ValueError(f"severity must be one of Severity enum")
        if not self.reason:
            raise ValueError("reason must not be empty")
        if not self.recommendation:
            raise ValueError("recommendation must not be empty")
        if not self.model:
            raise ValueError("model must not be empty")
