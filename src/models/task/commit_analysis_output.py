"""``CommitAnalysisOutput`` — результат ``COMMIT_ANALYSIS``."""

from dataclasses import dataclass

from ..common.codec import from_jsonable, to_jsonable


@dataclass(frozen=True, slots=True)
class CommitAnalysisOutput:
    """Сводка по пачке: обработанные/упавшие коммиты, кандидаты и порождённые задачи/инциденты."""

    commit_ids_done: list[int]
    commit_ids_failed: list[int]
    lines_analyzed: int
    candidates_total: int
    suspicious: int
    ai_task_ids: list[int]
    incident_ids: list[int]

    def to_json_dict(self) -> dict:
        """Сериализация в JSON-совместимый dict."""
        return to_jsonable(self)

    @classmethod
    def from_json_dict(cls, data: dict) -> "CommitAnalysisOutput":
        """Восстановление из JSON-dict."""
        return from_jsonable(cls, data)
