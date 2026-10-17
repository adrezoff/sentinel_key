"""``CommitAnalysisPayload`` (JSON value object for ``COMMIT_ANALYSIS``)."""

from dataclasses import dataclass

from ..common.codec import from_jsonable, to_jsonable


@dataclass(frozen=True, slots=True)
class CommitAnalysisPayload:
    commit_ids: list[int]

    def to_json_dict(self) -> dict:
        return to_jsonable(self)

    @classmethod
    def from_json_dict(cls, data: dict) -> "CommitAnalysisPayload":
        return from_jsonable(cls, data)
