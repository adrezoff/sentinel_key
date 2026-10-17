"""``CommitAnalysisOutput`` — output of ``COMMIT_ANALYSIS``."""

from dataclasses import dataclass

from ..common.codec import from_jsonable, to_jsonable


@dataclass(frozen=True, slots=True)
class CommitAnalysisOutput:
    commit_ids_done: list[int]
    commit_ids_failed: list[int]
    lines_analyzed: int
    candidates_total: int
    suspicious: int
    ai_task_ids: list[int]
    incident_ids: list[int]

    def to_json_dict(self) -> dict:
        return to_jsonable(self)

    @classmethod
    def from_json_dict(cls, data: dict) -> "CommitAnalysisOutput":
        return from_jsonable(cls, data)
