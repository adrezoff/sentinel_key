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


class MaskingViolationError(ValueError):
    """Нарушение маскирования: в строке обнаружен потенциальный сырой секрет."""

    def __init__(self, message: str, *, pattern_name: str | None = None, matched: str | None = None) -> None:
        super().__init__(message)
        self.pattern_name = pattern_name
        self.matched = matched


# ---------------------------------------------------------------------------
# Паттерны сырых секретов (компилируются один раз)
# ---------------------------------------------------------------------------

# AWS Access Key ID: AKIA... или ASIA... (20 символов)
AWS_ACCESS_KEY_PATTERN = re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")

# AWS Secret Access Key: 40 символов base64-совместимых
AWS_SECRET_KEY_PATTERN = re.compile(r"\b[A-Za-z0-9/+=]{40}\b")

# GitHub Personal Access Token: ghp_... или github_pat_...
GITHUB_TOKEN_PATTERN = re.compile(r"\b(ghp|github_pat)_[A-Za-z0-9_]{36,}\b")

# Slack Token: xox[baprs]...
SLACK_TOKEN_PATTERN = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")

# JWT: три base64url части через точку
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")

# Private Key: -----BEGIN ... PRIVATE KEY-----
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN\s+[A-Z\s]+PRIVATE KEY-----")

# Bearer Token: Authorization: Bearer <token>
BEARER_TOKEN_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b", re.IGNORECASE)

# Database URL с паролем: postgresql://user:pass@...
DB_URL_WITH_PASSWORD_PATTERN = re.compile(
    r"\b(postgresql|mysql|mongodb|redis)://[^:]+:[^@\s]+@[^\s]+\b"
)

# Generic API Key: api_key=... или apikey=...
API_KEY_PATTERN = re.compile(r"\b(api[_-]?key|apikey)\s*[:=]\s*[A-Za-z0-9_-]{20,}\b", re.IGNORECASE)

# Generic Password: password=... или pwd=...
PASSWORD_PATTERN = re.compile(r"\b(password|pwd|pass)\s*[:=]\s*\S{8,}\b", re.IGNORECASE)

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
    fingerprint_matches = re.findall(r"\b[a-fA-F0-9]{64}\b", text)
    for fp in fingerprint_matches:
        violations.append(MaskingViolationError(
            f"Detected potential value_fingerprint (64 hex chars): {fp[:16]}...",
            pattern_name="VALUE_FINGERPRINT",
            matched=fp,
        ))

    # 2. Проверка известных паттернов сырых секретов
    for name, pattern in RAW_SECRET_PATTERNS:
        matches = pattern.findall(text)
        for match in matches:
            # Маскируем совпадение в логе
            masked = match[:8] + "..." + match[-4:] if len(match) > 12 else "***"
            violations.append(MaskingViolationError(
                f"Detected raw secret pattern '{name}': {masked}",
                pattern_name=name,
                matched=match,
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
        # Объединяем все нарушения в одно сообщение
        details = "; ".join(f"{v.pattern_name}: {v.matched[:16]}..." for v in result.violations)
        raise MaskingViolationError(f"Masking violations detected: {details}")


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