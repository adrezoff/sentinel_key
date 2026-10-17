"""``ScoringConfig`` — frozen weights and route thresholds."""

from dataclasses import dataclass

from models.candidate import Route, ScoreBreakdown


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    weight_regex: float = 0.5
    weight_entropy: float = 0.2
    weight_context: float = 0.2
    weight_token_type: float = 0.1
    low_threshold: float = 0.3
    high_threshold: float = 0.7

    def route_for(self, score_total: float) -> Route:
        if score_total >= self.high_threshold:
            return Route.AI_VALIDATION
        if score_total >= self.low_threshold:
            return Route.SUSPICIOUS
        return Route.IGNORE

    def weighted(self, *, regex: float, entropy: float, context: float, token_type: float) -> ScoreBreakdown:
        return ScoreBreakdown(
            regex=regex * self.weight_regex,
            entropy=entropy * self.weight_entropy,
            context=context * self.weight_context,
            token_type=token_type * self.weight_token_type,
        )
