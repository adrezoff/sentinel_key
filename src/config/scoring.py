"""Неизменяемые веса, пороги и ограничения статического анализа."""

from dataclasses import dataclass

from models.candidate import Route, ScoreBreakdown


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    """Настройки признаков и маршрутизации без обращения к окружению."""

    weight_regex: float = 0.5
    regex_rule_weights: tuple[tuple[str, float], ...] = (
        ("aws_access_key", 1.0),
        ("github_token", 1.0),
        ("slack_token", 1.0),
        ("jwt", 0.9),
        ("private_key", 1.0),
        ("bearer_token", 0.6),
        ("database_url_password", 0.8),
        ("aws_secret_key", 0.9),
        ("generic_api_key", 0.5),
        ("generic_password", 0.5),
    )
    weight_entropy: float = 0.2
    weight_context: float = 0.2
    weight_token_type: float = 0.1
    low_threshold: float = 0.3
    high_threshold: float = 0.7
    min_entropy_length: int = 16
    entropy_hex_threshold: float = 3.5
    entropy_base64_threshold: float = 4.5
    entropy_other_threshold: float = 3.5
    max_analyzed_length: int = 4096
    test_context_factor: float = 0.25
    false_positive_factor: float = 0.1

    def __post_init__(self) -> None:
        """Проверить диапазоны, от которых зависит корректная маршрутизация."""
        weights = (self.weight_regex, self.weight_entropy, self.weight_context, self.weight_token_type)
        factors = (self.test_context_factor, self.false_positive_factor)
        if any(not 0 <= value <= 1 for value in (*weights, *factors)):
            raise ValueError("weights and factors must be between 0 and 1")
        names = [name for name, _weight in self.regex_rule_weights]
        if len(names) != len(set(names)) or any(not 0 <= weight <= 1 for _name, weight in self.regex_rule_weights):
            raise ValueError("regex rule weights must have unique names and values between 0 and 1")
        if not 0 <= self.low_threshold <= self.high_threshold <= 1:
            raise ValueError("route thresholds must be ordered between 0 and 1")
        if self.min_entropy_length < 1 or self.max_analyzed_length < self.min_entropy_length:
            raise ValueError("invalid analysis length limits")
        if any(value < 0 for value in (self.entropy_hex_threshold, self.entropy_base64_threshold, self.entropy_other_threshold)):
            raise ValueError("entropy thresholds must be non-negative")

    def route_for(self, score_total: float) -> Route:
        """Выбрать маршрут по порогам для включённой AI-валидации."""
        if score_total >= self.high_threshold:
            return Route.AI_VALIDATION
        if score_total >= self.low_threshold:
            return Route.SUSPICIOUS
        return Route.IGNORE

    def rule_weight(self, name: str) -> float:
        """Вернуть относительную силу правила из явного конфига."""
        for rule_name, weight in self.regex_rule_weights:
            if rule_name == name:
                return weight
        raise ValueError("unknown regex rule")

    def weighted(self, *, regex: float, entropy: float, context: float, token_type: float) -> ScoreBreakdown:
        """Применить веса к нормированным локальным признакам."""
        return ScoreBreakdown(
            regex=regex * self.weight_regex,
            entropy=entropy * self.weight_entropy,
            context=context * self.weight_context,
            token_type=token_type * self.weight_token_type,
        )
