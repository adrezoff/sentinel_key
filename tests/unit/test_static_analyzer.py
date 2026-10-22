"""Проверки детекторов, маршрута и границы между сырым и сохраняемым."""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import ScoringConfig
from models import DetectorFlags, RawCandidate, Route, SecretType
from static_analyzer import StaticAnalyzer
from static_analyzer.entropy import shannon_entropy


def _raw(value: str, *, key: str | None = None, path: str = "src/settings.py", hint: SecretType = SecretType.UNKNOWN, context: list[str] | None = None) -> RawCandidate:
    return RawCandidate(
        repository_id=1,
        commit_id=2,
        file_path=path,
        line_number=3,
        raw_value=value,
        source_line_raw=f"{key or 'value'}={value}",
        context_raw=context or [],
        source_line_masked=f"{key or 'value'}=****",
        context_masked="",
        secret_type_hint=hint,
        value_fingerprint=b"\x01" * 32,
        value_masked="****",
        key_name=key,
    )


class StaticAnalyzerTests(unittest.TestCase):
    """Проверить локальную классификацию без БД и внешних вызовов."""

    def setUp(self) -> None:
        """Создать анализатор с документированными настройками."""
        self.analyzer = StaticAnalyzer()

    def test_three_reliable_formats(self) -> None:
        """Три самостоятельных формата дают ожидаемые типы и правила."""
        examples = (
            ("AKIAIOSFODNN7EXAMPLE", SecretType.AWS_ACCESS_KEY, "aws_access_key"),
            ("ghp_" + "A1b2" * 9, SecretType.GITHUB_TOKEN, "github_token"),
            ("xoxb-" + "1234567890" * 2, SecretType.SLACK_TOKEN, "slack_token"),
        )
        for value, expected_type, rule in examples:
            with self.subTest(rule=rule):
                candidate = self.analyzer.analyze(_raw(value))
                self.assertEqual(candidate.secret_type, expected_type)
                self.assertIn(rule, candidate.regex_rules)
                self.assertTrue(candidate.detector_flags & DetectorFlags.REGEX)
                self.assertNotIn(value, repr(candidate))

    def test_three_false_positives(self) -> None:
        """UUID, hash и placeholder понижают балл без удаления модели."""
        examples = (
            "123e4567-e89b-12d3-a456-426614174000",
            "abcdef0123456789" * 4,
            "<your-key>",
        )
        for value in examples:
            with self.subTest(value=value):
                candidate = self.analyzer.analyze(_raw(value, key="api_key"))
                self.assertEqual(candidate.route, Route.IGNORE)
                self.assertLess(candidate.score_total, self.analyzer.config.low_threshold)

    def test_context_and_route(self) -> None:
        """Тестовый путь понижает балл; AI-флаг управляет верхним маршрутом."""
        value = "ghp_" + "A1b2" * 9
        production = self.analyzer.analyze(_raw(value, key="api_key"), ai_enabled=True)
        local = self.analyzer.analyze(_raw(value, key="api_key"))
        test = self.analyzer.analyze(_raw(value, key="api_key", path="tests/fixtures/settings.py"), ai_enabled=True)
        self.assertEqual(production.route, Route.AI_VALIDATION)
        self.assertEqual(local.route, Route.SUSPICIOUS)
        self.assertTrue(test.is_test_context)
        self.assertLess(test.score_total, production.score_total)

    def test_entropy_and_score_components(self) -> None:
        """Энтропия входит в отдельный компонент, сумма совпадает с моделью."""
        value = "Aa01+/Bb23=/Cc45Dd67Ee89Ff"
        candidate = self.analyzer.analyze(_raw(value))
        self.assertGreater(shannon_entropy(value), 0)
        self.assertAlmostEqual(candidate.score_total, candidate.score.total)
        self.assertEqual(bool(candidate.detector_flags & DetectorFlags.ENTROPY), candidate.score_entropy > 0)

    def test_rule_weight_comes_from_config(self) -> None:
        """Изменение веса правила меняет только вклад regex и маршрут."""
        value = "AKIAIOSFODNN7EXAMPLE"
        baseline = self.analyzer.analyze(_raw(value))
        weights = tuple((name, 0.0 if name == "aws_access_key" else weight) for name, weight in self.analyzer.config.regex_rule_weights)
        adjusted = StaticAnalyzer(ScoringConfig(regex_rule_weights=weights)).analyze(_raw(value))
        self.assertGreater(baseline.score_regex, adjusted.score_regex)
        self.assertEqual(adjusted.score_regex, 0.0)

    def test_async_and_length_bound(self) -> None:
        """Асинхронный путь эквивалентен синхронному и ограничивает длинный ввод."""
        raw = _raw("a" * 100)
        config = ScoringConfig(max_analyzed_length=32)
        analyzer = StaticAnalyzer(config)
        created = datetime(2026, 1, 1, tzinfo=timezone.utc)
        sync = analyzer.analyze(raw, created_at=created)
        async_result = asyncio.run(analyzer.analyze_async(raw, created_at=created))
        self.assertEqual(sync, async_result)
        self.assertEqual(sync.entropy, 0.0)


if __name__ == "__main__":
    unittest.main()
