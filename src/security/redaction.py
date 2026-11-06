"""Общие правила обнаружения и санитизации потенциальных сырых секретов."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RedactionPattern:
    """Имя паттерна, регулярное выражение и безопасная замена."""

    name: str
    pattern: re.Pattern[str]
    replacement: str = "***MASKED***"


AWS_ACCESS_KEY_PATTERN = RedactionPattern(
    "AWS_ACCESS_KEY",
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "AKIA***MASKED***",
)
AWS_SECRET_KEY_PATTERN = RedactionPattern(
    "AWS_SECRET_KEY",
    re.compile(r"\b[A-Za-z0-9/+=]{40}\b"),
)
GITHUB_TOKEN_PATTERN = RedactionPattern(
    "GITHUB_TOKEN",
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{36,}\b"),
    "ghp_***MASKED***",
)
SLACK_TOKEN_PATTERN = RedactionPattern(
    "SLACK_TOKEN",
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "xox***MASKED***",
)
JWT_PATTERN = RedactionPattern(
    "JWT",
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    "JWT.***MASKED***",
)
PRIVATE_KEY_PATTERN = RedactionPattern(
    "PRIVATE_KEY",
    re.compile(r"-----BEGIN\s+[A-Z\s]+PRIVATE KEY-----"),
)
BEARER_TOKEN_PATTERN = RedactionPattern(
    "BEARER_TOKEN",
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b", re.IGNORECASE),
)
DB_URL_WITH_PASSWORD_PATTERN = RedactionPattern(
    "DB_URL_WITH_PASSWORD",
    re.compile(
        r"\b(?:postgresql|mysql|mongodb|redis)://[^:]+:[^@\s]+@[^\s]+\b",
        re.IGNORECASE,
    ),
)
API_KEY_PATTERN = RedactionPattern(
    "API_KEY",
    re.compile(
        r"\b(?:api[_-]?key|apikey)\s*[:=]\s*[A-Za-z0-9_-]{20,}\b",
        re.IGNORECASE,
    ),
)
PASSWORD_PATTERN = RedactionPattern(
    "PASSWORD",
    re.compile(r"\b(?:password|pwd|pass)\s*[:=]\s*\S{8,}\b", re.IGNORECASE),
)
VALUE_FINGERPRINT_PATTERN = RedactionPattern(
    "VALUE_FINGERPRINT",
    re.compile(r"\b[a-fA-F0-9]{64}\b"),
)

REDACTION_PATTERNS: tuple[RedactionPattern, ...] = (
    AWS_ACCESS_KEY_PATTERN,
    AWS_SECRET_KEY_PATTERN,
    GITHUB_TOKEN_PATTERN,
    SLACK_TOKEN_PATTERN,
    JWT_PATTERN,
    PRIVATE_KEY_PATTERN,
    BEARER_TOKEN_PATTERN,
    DB_URL_WITH_PASSWORD_PATTERN,
    API_KEY_PATTERN,
    PASSWORD_PATTERN,
    VALUE_FINGERPRINT_PATTERN,
)


def is_hex_only_aws_secret(value: str) -> bool:
    """Проверить, является ли 40-символьное значение hex-only (например, SHA-1)."""
    return bool(re.fullmatch(r"[A-Fa-f0-9]{40}", value))


def redact_text(text: str) -> str:
    """Заменить все поддерживаемые вхождения потенциальных секретов."""
    sanitized = text
    for item in REDACTION_PATTERNS:
        def replace(match: re.Match[str]) -> str:
            value = match.group(0)
            if item.name == "AWS_SECRET_KEY" and is_hex_only_aws_secret(value):
                return value
            return item.replacement

        sanitized = item.pattern.sub(replace, sanitized)
    return sanitized


def contains_secret(text: str) -> bool:
    """Проверить наличие поддерживаемого секрета без раскрытия совпадения."""
    return redact_text(text) != text
