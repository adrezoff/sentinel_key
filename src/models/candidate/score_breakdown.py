"""``ScoreBreakdown`` — value object, разворачивается в колонки ``candidates.score_*``."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Составляющие риск-скора статического анализатора.

    ``total`` — сумма вкладов, обрезанная сверху до 1.0.
    """

    regex: float
    entropy: float
    context: float
    token_type: float

    @property
    def total(self) -> float:
        """Итоговый скор ``min(1.0, сумма вкладов)``."""
        return min(1.0, self.regex + self.entropy + self.context + self.token_type)
