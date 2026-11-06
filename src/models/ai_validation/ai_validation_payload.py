"""``AiValidationPayload`` — JSON value object для ``AI_VALIDATION``."""

from __future__ import annotations

from dataclasses import dataclass

from ..common.codec import from_jsonable, to_jsonable


@dataclass(frozen=True, slots=True)
class AiValidationPayload:
    """Параметры задачи ``AI_VALIDATION``: ``candidate_id`` и ``prompt_version``.

    ``candidate_id`` + ``prompt_version`` образуют ``idempotency_key`` —
    повторная отправка одной кандидатой с тем же prompt-видом затирается.
    ``model`` опционально — переопределение провайдера ИИ; ``None`` = из конфига.
    """

    candidate_id: int
    prompt_version: int
    model: str | None = None

    def to_json_dict(self) -> dict:
        """Сериализация в JSON-совместимый dict."""
        return to_jsonable(self)

    @classmethod
    def from_json_dict(cls, data: dict) -> "AiValidationPayload":
        """Восстановление из JSON-dict."""
        return from_jsonable(cls, data)
