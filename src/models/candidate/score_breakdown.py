"""``ScoreBreakdown`` value object (unfolded into ``candidates.score_*``)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    regex: float
    entropy: float
    context: float
    token_type: float

    @property
    def total(self) -> float:
        return min(1.0, self.regex + self.entropy + self.context + self.token_type)
