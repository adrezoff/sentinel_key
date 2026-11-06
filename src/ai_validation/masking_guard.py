# -*- coding: utf-8 -*-
"""Masking Guard: защита от утечки сырых секретов в LLM.

Проверяет, что в промпте нет:
- value_fingerprint (HMAC-SHA256)
- Сырых паттернов секретов (AWS keys, GitHub tokens, private keys и т.д.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from models.candidate import Candidate
from security.redaction import (
    API_KEY_PATTERN as _API_KEY_PATTERN,
    AWS_ACCESS_KEY_PATTERN as _AWS_ACCESS_KEY_PATTERN,
    AWS_SECRET_KEY_PATTERN as _AWS_SECRET_KEY_PATTERN,
    BEARER_TOKEN_PATTERN as _BEARER_TOKEN_PATTERN,
    DB_URL_WITH_PASSWORD_PATTERN as _DB_URL_WITH_PASSWORD_PATTERN,
    GITHUB_TOKEN_PATTERN as _GITHUB_TOKEN_PATTERN,
    JWT_PATTERN as _JWT_PATTERN,
    PASSWORD_PATTERN as _PASSWORD_PATTERN,
    PRIVATE_KEY_PATTERN as _PRIVATE_KEY_PATTERN,
    SLACK_TOKEN_PATTERN as _SLACK_TOKEN_PATTERN,
    VALUE_FINGERPRINT_PATTERN as _VALUE_FINGERPRINT_PATTERN,
    is_hex_only_aws_secret,
)


class MaskingViolationError(ValueError):
    """Нарушение маскирования: в строке обнаружен потенциальный сырой секрет."""

    def __init__(self, message: str, *, pattern_name: str | None = None) -> None:
        super().__init__(message)
        self.pattern_name = pattern_name


# ---------------------------------------------------------------------------
# Паттерны сырых секретов (компилируются один раз)
# ---------------------------------------------------------------------------

AWS_ACCESS_KEY_PATTERN = _AWS_ACCESS_KEY_PATTERN.pattern
AWS_SECRET_KEY_PATTERN = _AWS_SECRET_KEY_PATTERN.pattern
GITHUB_TOKEN_PATTERN = _GITHUB_TOKEN_PATTERN.pattern
SLACK_TOKEN_PATTERN = _SLACK_TOKEN_PATTERN.pattern
JWT_PATTERN = _JWT_PATTERN.pattern
PRIVATE_KEY_PATTERN = _PRIVATE_KEY_PATTERN.pattern
BEARER_TOKEN_PATTERN = _BEARER_TOKEN_PATTERN.pattern
DB_URL_WITH_PASSWORD_PATTERN = _DB_URL_WITH_PASSWORD_PATTERN.pattern
API_KEY_PATTERN = _API_KEY_PATTERN.pattern
PASSWORD_PATTERN = _PASSWORD_PATTERN.pattern
VALUE_FINGERPRINT_PATTERN = _VALUE_FINGERPRINT_PATTERN.pattern

# Сводный список паттернов для проверки
RAW_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS_ACCESS_KEY", AWS_ACCESS_KEY_PATTERN),
    ("AWS_SECRET_KEY", AWS_SECRET_KEY_PATTERN),
    ("GITHUB_TOKEN", GITHUB_TOKEN_PATTERN),
    ("SLACK_TOKEN", SLACK_TOKEN_PATTERN),
    ("JWT", JWT_PATTERN),
    ("PRIVATE_KEY", PRIVATE_KEY_PATTERN),
    ("BEARER_TOKEN", BEARER_TOKEN_PATTERN),
    ("DB_URL_WITH_PASSWORD", DB_URL_WITH_PASSWORD_PATTERN),
    ("API_KEY", API_KEY_PATTERN),
    ("PASSWORD", PASSWORD_PATTERN),
]


@dataclass(frozen=True, slots=True)
class MaskingGuardResult:
    """Результат проверки маскирования."""

    ok: bool
    violations: list[MaskingViolationError]


def check_masking(text: str) -> MaskingGuardResult:
    """Проверить текст на наличие сырых секретов.

    Args:
        text: Текст для проверки (промпт, контекст и т.д.)

    Returns:
        MaskingGuardResult с флагом ok и списком нарушений.
    """
    violations: list[MaskingViolationError] = []

    # 1. Проверка value_fingerprint (hex-строка 64 символа = 32 байта)
    fingerprint_matches = VALUE_FINGERPRINT_PATTERN.findall(text)
    for fp in fingerprint_matches:
        violations.append(MaskingViolationError(
            "Detected potential value_fingerprint (64 hex chars)",
            pattern_name="VALUE_FINGERPRINT",
        ))

    # 2. Проверка известных паттернов сырых секретов
    for name, pattern in RAW_SECRET_PATTERNS:
        for match in pattern.findall(text):
            # Hex-only значения не являются AWS Secret Access Key: так часто выглядят SHA-1.
            if name == "AWS_SECRET_KEY" and is_hex_only_aws_secret(match):
                continue
            violations.append(MaskingViolationError(
                f"Detected raw secret pattern '{name}'",
                pattern_name=name,
            ))

    return MaskingGuardResult(ok=len(violations) == 0, violations=violations)


def assert_safe_for_llm(text: str) -> None:
    """Проверить текст и выбросить исключение, если найдены нарушения.

    Использовать перед отправкой промпта в LLM.

    Args:
        text: Текст для проверки.

    Raises:
        MaskingViolationError: если обнаружены сырые секреты.
    """
    result = check_masking(text)
    if not result.ok:
        names = ", ".join(
            violation.pattern_name or "UNKNOWN"
            for violation in result.violations
        )
        raise MaskingViolationError(f"Masking violations detected: {names}")


def check_candidate_masking(candidate: Candidate) -> MaskingGuardResult:
    """Проверить кандидата на корректность маскирования.

    Args:
        candidate: Кандидат с уже замаскированными полями.

    Returns:
        Результат проверки полей кандидата.
    """
    # Проверяем поля, которые должны быть замаскированы
    combined = " ".join([
        candidate.value_masked,
        candidate.source_line_masked,
        candidate.context_masked,
    ])
    return check_masking(combined)