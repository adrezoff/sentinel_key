"""Политика подтверждения инцидента по кандидату и вердикту."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from models.candidate import Candidate
from models.enums import SecretType, Severity


class AiVerdictLike(Protocol):
    """Минимальный контракт вердикта до появления полной модели AI-ветки."""

    is_secret: bool
    confidence: float


def _default_trusted_formats() -> frozenset[SecretType]:
    return frozenset(
        {
            SecretType.PRIVATE_KEY,
            SecretType.AWS_ACCESS_KEY,
            SecretType.GITHUB_TOKEN,
            SecretType.SLACK_TOKEN,
        }
    )


@dataclass(frozen=True, slots=True)
class IncidentPolicy:
    """Пороги для локального решения и дальнейшей AI-ветки."""

    min_ai_confidence: float = 0.85
    trusted_formats: frozenset[SecretType] = field(default_factory=_default_trusted_formats)
    min_local_score: float = 0.9
    bump_if_in_head: bool = True

    def should_open(self, candidate: Candidate, verdict: AiVerdictLike | None = None) -> bool:
        """Применить правила из ``docs/models.md`` §3.9."""
        if verdict is not None:
            return verdict.is_secret and verdict.confidence >= self.min_ai_confidence
        return (
            candidate.secret_type in self.trusted_formats
            and candidate.score_total >= self.min_local_score
            and not candidate.is_test_context
        )

    def bump_severity(self, severity: Severity, *, still_in_head: bool | None) -> Severity:
        """Повысить уровень на один, если секрет остаётся в HEAD."""
        if not (self.bump_if_in_head and still_in_head):
            return severity
        if severity >= Severity.CRITICAL:
            return Severity.CRITICAL
        return Severity(int(severity) + 1)
