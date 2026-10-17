"""``ScanTaskPayload`` (JSON value object for ``REPOSITORY_SCAN``)."""

from dataclasses import dataclass, field
from enum import IntEnum

from ..common.codec import from_jsonable, to_jsonable


class ScanMode(IntEnum):
    FULL = 1
    INCREMENTAL = 2
    TARGETED = 3


class Trigger(IntEnum):
    MANUAL = 1
    SCHEDULE = 2
    WEBHOOK = 3


def _default_refs() -> list[str]:
    return ["--all"]


@dataclass(frozen=True, slots=True)
class ScanTaskPayload:
    mode: ScanMode = ScanMode.INCREMENTAL
    trigger: Trigger = Trigger.MANUAL
    batch_size: int = 50
    refs: list[str] = field(default_factory=_default_refs)
    target_shas: list[str] = field(default_factory=list)
    ai_enabled: bool = True

    def to_json_dict(self) -> dict:
        return to_jsonable(self)

    @classmethod
    def from_json_dict(cls, data: dict) -> "ScanTaskPayload":
        return from_jsonable(cls, data)
