"""``CommitAnalysisPayload`` — JSON value object для ``COMMIT_ANALYSIS``."""

from dataclasses import dataclass

from ..common.codec import from_jsonable, to_jsonable


@dataclass(frozen=True, slots=True)
class CommitAnalysisPayload:
    """Параметры задачи анализа пачки коммитов: список ``commits.id``."""

    commit_ids: list[int]

    def to_json_dict(self) -> dict:
        """Сериализация в JSON-совместимый dict."""
        return to_jsonable(self)

    @classmethod
    def from_json_dict(cls, data: dict) -> "CommitAnalysisPayload":
        """Восстановление из JSON-dict."""
        return from_jsonable(cls, data)
