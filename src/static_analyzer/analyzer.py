"""Локальная классификация кандидатов без БД, сети и побочных эффектов."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from config import ScoringConfig
from models import Candidate, DetectorFlags, RawCandidate, Route, SecretType

from .detectors import is_false_positive, matches
from .entropy import entropy_threshold, shannon_entropy

_TEST_PARTS = frozenset({"test", "tests", "fixture", "fixtures", "docs", "doc", "examples", "example"})
_SENSITIVE_PARTS = frozenset({"config", "configs", "secrets", "credentials"})
_SENSITIVE_KEY = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|credential|private[_-]?key)", re.IGNORECASE)
_TEST_CONTEXT = re.compile(r"(?:\b(?:test|mock|fixture|sample|example|dummy)\b|assert|pytest)", re.IGNORECASE)


class StaticAnalyzer:
    """Получает сырой кандидат и возвращает только безопасную сохраняемую модель."""

    def __init__(self, config: ScoringConfig | None = None) -> None:
        """Использовать явно переданную или документированную конфигурацию."""
        self.config = config if config is not None else ScoringConfig()

    def analyze(
        self,
        raw: RawCandidate,
        *,
        ai_enabled: bool = False,
        created_at: datetime | None = None,
    ) -> Candidate:
        """Вычислить независимые признаки и маршрут без сохранения секрета."""
        config = self.config
        value = raw.raw_value
        bounded = len(value) <= config.max_analyzed_length
        rules = matches(raw, config.max_analyzed_length)
        regex_signal = max((config.rule_weight(rule.name) for rule in rules), default=0.0)
        entropy = shannon_entropy(value) if bounded else 0.0
        entropy_signal = float(
            bounded
            and len(value) >= config.min_entropy_length
            and entropy >= entropy_threshold(value, config)
        )

        path = raw.file_path.replace("\\", "/").lower()
        parts = set(path.split("/"))
        key = raw.key_name or ""
        test_context = bool(parts & _TEST_PARTS or "example" in path or _TEST_CONTEXT.search(key))
        if not test_context:
            test_context = any(_TEST_CONTEXT.search(line[:config.max_analyzed_length]) for line in raw.context_raw[:4])
        context_signal = float(bool(_SENSITIVE_KEY.search(key) or parts & _SENSITIVE_PARTS or path.endswith((".env", ".pem", ".key"))))
        token_type_signal = float(raw.secret_type_hint is not SecretType.UNKNOWN or bool(rules))

        factor = 1.0
        if test_context:
            factor *= config.test_context_factor
        if bounded and is_false_positive(value):
            factor *= config.false_positive_factor

        score = config.weighted(
            regex=regex_signal * factor,
            entropy=entropy_signal * factor,
            context=context_signal * factor,
            token_type=token_type_signal * factor,
        )
        flags = DetectorFlags(0)
        if rules:
            flags |= DetectorFlags.REGEX
        if entropy_signal:
            flags |= DetectorFlags.ENTROPY
        if context_signal:
            flags |= DetectorFlags.CONTEXT

        route = config.route_for(score.total)
        if route is Route.AI_VALIDATION and not ai_enabled:
            route = Route.SUSPICIOUS
        secret_type = max(rules, key=lambda rule: config.rule_weight(rule.name)).secret_type if rules else raw.secret_type_hint
        return raw.to_candidate(
            score,
            flags,
            route,
            entropy=entropy,
            regex_rules=[rule.name for rule in rules],
            is_test_context=test_context,
            secret_type=secret_type,
            created_at=created_at,
        )

    async def analyze_async(
        self,
        raw: RawCandidate,
        *,
        ai_enabled: bool = False,
        created_at: datetime | None = None,
    ) -> Candidate:
        """Вынести CPU-расчёт из event loop для рабочего pipeline."""
        return await asyncio.to_thread(self.analyze, raw, ai_enabled=ai_enabled, created_at=created_at)
