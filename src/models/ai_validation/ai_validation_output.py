"""``AiValidationOutput`` — результат ``AI_VALIDATION``."""

from __future__ import annotations

from dataclasses import dataclass

from ..common.codec import from_jsonable, to_jsonable


@dataclass(frozen=True, slots=True)
class AiValidationOutput:
    """Итог ИИ-валидации: ``verdict_id`` и опциональный ``incident_id``.

    ``verdict_id`` — id сохранённого ``AiVerdict``. ``incident_id`` — ``None``,
    если инцидент не открыт (вердикт не секрет или confiance ниже порога).
    """

    verdict_id: int
    incident_id: int | None = None

    def to_json_dict(self) -> dict:
        """Сериализация в JSON-совместимый dict."""
        return to_jsonable(self)

    @classmethod
    def from_json_dict(cls, data: dict) -> "AiValidationOutput":
        """Восстановление из JSON-dict."""
        return from_jsonable(cls, data)
