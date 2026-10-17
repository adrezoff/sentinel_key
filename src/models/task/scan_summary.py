"""``ScanSummary`` — output of ``REPOSITORY_SCAN``."""

from dataclasses import dataclass

from ..common.codec import from_jsonable, to_jsonable
from .scan_task_payload import ScanMode


@dataclass(frozen=True, slots=True)
class ScanSummary:
    mode: ScanMode
    commits_total: int
    commits_new: int
    commits_done: int
    commits_failed: int
    candidates: int
    ai_tasks: int
    ai_failed: int
    incidents_opened: int
    incidents_updated: int

    def to_json_dict(self) -> dict:
        return to_jsonable(self)

    @classmethod
    def from_json_dict(cls, data: dict) -> "ScanSummary":
        return from_jsonable(cls, data)
