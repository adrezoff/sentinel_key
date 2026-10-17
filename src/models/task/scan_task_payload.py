"""``ScanTaskPayload`` — JSON value object для ``REPOSITORY_SCAN``."""

from dataclasses import dataclass, field
from enum import IntEnum

from ..common.codec import from_jsonable, to_jsonable


class ScanMode(IntEnum):
    """Режим запуска: полная история, инкремент или целевые SHA."""

    FULL = 1
    INCREMENTAL = 2
    TARGETED = 3


class Trigger(IntEnum):
    """Источник запуска сканирования."""

    MANUAL = 1
    SCHEDULE = 2
    WEBHOOK = 3


def _default_refs() -> list[str]:
    return ["--all"]


@dataclass(frozen=True, slots=True)
class ScanTaskPayload:
    """Параметры корневой задачи запуска анализа репозитория."""

    mode: ScanMode = ScanMode.INCREMENTAL
    trigger: Trigger = Trigger.MANUAL
    batch_size: int = 50
    refs: list[str] = field(default_factory=_default_refs)
    target_shas: list[str] = field(default_factory=list)
    ai_enabled: bool = True

    def to_json_dict(self) -> dict:
        """Сериализация в JSON-совместимый dict."""
        return to_jsonable(self)

    @classmethod
    def from_json_dict(cls, data: dict) -> "ScanTaskPayload":
        """Восстановление из JSON-dict."""
        return from_jsonable(cls, data)
