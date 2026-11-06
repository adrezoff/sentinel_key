# -*- coding: utf-8 -*-
"""Unit tests for masking_guard."""

from __future__ import annotations

import unittest

from ai_validation.masking_guard import (
    MaskingViolationError,
    check_masking,
    assert_safe_for_llm,
    check_candidate_masking,
    RAW_SECRET_PATTERNS,
)


class TestMaskingGuard(unittest.TestCase):
    def test_clean_text_passes(self) -> None:
        """Чистый текст без секретов проходит проверку."""
        result = check_masking("This is a clean text with no secrets.")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.violations), 0)

    def test_aws_access_key_detected(self) -> None:
        """AWS Access Key ID обнаруживается."""
        text = "AKIAIOSFODNN7EXAMPLE"
        result = check_masking(text)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.violations), 1)
        self.assertEqual(result.violations[0].pattern_name, "AWS_ACCESS_KEY")

    def test_github_token_detected(self) -> None:
        """GitHub token обнаруживается."""
        text = "ghp_123456789012345678901234567890123456"
        result = check_masking(text)
        self.assertFalse(result.ok)
        self.assertEqual(result.violations[0].pattern_name, "GITHUB_TOKEN")

    def test_jwt_detected(self) -> None:
        """JWT токен обнаруживается."""
        text = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result = check_masking(text)
        self.assertFalse(result.ok)
        self.assertEqual(result.violations[0].pattern_name, "JWT")

    def test_private_key_detected(self) -> None:
        """Private key заголовок обнаруживается."""
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA..."
        result = check_masking(text)
        self.assertFalse(result.ok)
        self.assertEqual(result.violations[0].pattern_name, "PRIVATE_KEY")

    def test_value_fingerprint_detected(self) -> None:
        """64-символьный hex (value_fingerprint) обнаруживается."""
        text = "a" * 64  # 64 hex chars
        result = check_masking(text)
        self.assertFalse(result.ok)
        self.assertEqual(result.violations[0].pattern_name, "VALUE_FINGERPRINT")

    def test_multiple_violations_collected(self) -> None:
        """Несколько нарушений собираются в список."""
        text = "AKIAIOSFODNN7EXAMPLE and ghp_123456789012345678901234567890123456"
        result = check_masking(text)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.violations), 2)
        names = {v.pattern_name for v in result.violations}
        self.assertEqual(names, {"AWS_ACCESS_KEY", "GITHUB_TOKEN"})

    def test_assert_safe_for_llm_raises_on_violation(self) -> None:
        """assert_safe_for_llm бросает исключение при нарушении."""
        with self.assertRaises(MaskingViolationError):
            assert_safe_for_llm("AKIAIOSFODNN7EXAMPLE")

    def test_assert_safe_for_llm_passes_on_clean(self) -> None:
        """assert_safe_for_llm не бросает исключение на чистом тексте."""
        assert_safe_for_llm("Clean text")  # не должно бросать

    def test_candidate_masking_check(self) -> None:
        """Проверка маскирования кандидата."""
        from models.candidate import Candidate, Route
        from models.enums import SecretType, DetectorFlags

        candidate = Candidate(
            id=1,
            repository_id=1,
            commit_id=1,
            file_path="config.py",
            line_number=10,
            key_name="api_key",
            secret_type=SecretType.API_KEY,
            value_masked="AKIA***EXAMPLE",  # замаскированный
            value_fingerprint=b"x" * 32,
            entropy=3.5,
            regex_rules=[],
            detector_flags=DetectorFlags.REGEX,
            source_line_masked="api_key = \"AKIA***EXAMPLE\"",
            context_masked="api_key = \"AKIA***EXAMPLE\"\nother = 123",
            is_test_context=False,
            score_regex=0.8,
            score_entropy=0.3,
            score_context=0.2,
            score_token_type=0.5,
            score_total=0.6,
            route=Route.AI_VALIDATION,
        )
        # Маскированные значения должны пройти
        result = check_candidate_masking(candidate)
        self.assertTrue(result.ok)

    def test_candidate_with_raw_secret_fails(self) -> None:
        """Кандидат с сырым секретом в source_line не проходит."""
        from models.candidate import Candidate, Route
        from models.enums import SecretType, DetectorFlags

        candidate = Candidate(
            id=1,
            repository_id=1,
            commit_id=1,
            file_path="config.py",
            line_number=10,
            key_name="api_key",
            secret_type=SecretType.API_KEY,
            value_masked="AKIA***EXAMPLE",
            value_fingerprint=b"x" * 32,
            entropy=3.5,
            regex_rules=[],
            detector_flags=DetectorFlags.REGEX,
            source_line_masked="api_key = \"AKIAIOSFODNN7EXAMPLE\"",  # НЕ замаскирован!
            context_masked="api_key = \"AKIAIOSFODNN7EXAMPLE\"\nother = 123",
            is_test_context=False,
            score_regex=0.8,
            score_entropy=0.3,
            score_context=0.2,
            score_token_type=0.5,
            score_total=0.6,
            route=Route.AI_VALIDATION,
        )
        result = check_candidate_masking(candidate)
        self.assertFalse(result.ok)
        # Должен найти AWS_ACCESS_KEY в source_line_masked
        self.assertTrue(any(v.pattern_name == "AWS_ACCESS_KEY" for v in result.violations))


if __name__ == "__main__":
    unittest.main()