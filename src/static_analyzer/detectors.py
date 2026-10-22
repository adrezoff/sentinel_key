"""Ограниченный каталог форматов и фильтры известных ложных срабатываний."""

from __future__ import annotations

import re
from dataclasses import dataclass

from models import RawCandidate, SecretType


@dataclass(frozen=True, slots=True)
class RegexRule:
    """Имя правила, безопасный regex и тип; вес задаёт конфигурация."""

    name: str
    pattern: re.Pattern[str] | None
    secret_type: SecretType


RULES: tuple[RegexRule, ...] = (
    RegexRule("aws_access_key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"), SecretType.AWS_ACCESS_KEY),
    RegexRule("github_token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{20,255}"), SecretType.GITHUB_TOKEN),
    RegexRule("slack_token", re.compile(r"xox(?:b|p|a|r|s)-[A-Za-z0-9-]{10,255}"), SecretType.SLACK_TOKEN),
    RegexRule("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), SecretType.JWT),
    RegexRule("private_key", re.compile(r"\A-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"), SecretType.PRIVATE_KEY),
)

_UUID = re.compile(r"\A[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
_HEX_HASH = re.compile(r"\A[0-9a-fA-F]{32,128}\Z")
_TIMESTAMP = re.compile(r"\A\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-]+Z?)?\Z")
_PLACEHOLDER = re.compile(r"\A(?:x{4,}|\*{4,}|changeme|change_me|replace_me|your[_-]?(?:key|token|password|secret)|<[^>]{1,80}>|\$\{[^}]{1,80}\})\Z", re.IGNORECASE)
_PUBLIC = frozenset({"true", "false", "none", "null", "localhost", "127.0.0.1", "example", "example.com", "test", "password", "secret"})


def matches(raw: RawCandidate, max_length: int) -> list[RegexRule]:
    """Найти форматы в ограниченном значении и структурных подсказках."""
    value = raw.raw_value
    if len(value) > max_length:
        return []
    found = [rule for rule in RULES if rule.pattern is not None and rule.pattern.search(value)]
    hint = raw.secret_type_hint
    if hint is SecretType.BEARER_TOKEN and len(value) >= 8:
        found.append(RegexRule("bearer_token", None, hint))
    elif hint is SecretType.DATABASE_URL and len(value) >= 8:
        found.append(RegexRule("database_url_password", None, hint))
    elif hint is SecretType.PRIVATE_KEY and not any(rule.name == "private_key" for rule in found):
        found.append(RegexRule("private_key", None, hint))
    key = (raw.key_name or "").lower()
    if "aws_secret" in key and len(value) == 40 and all(char.isascii() and (char.isalnum() or char in "+/=") for char in value):
        found.append(RegexRule("aws_secret_key", None, SecretType.AWS_SECRET_KEY))
    elif any(part in key for part in ("api_key", "apikey", "access_key")) and len(value) >= 8:
        found.append(RegexRule("generic_api_key", None, SecretType.API_KEY))
    elif "password" in key and len(value) >= 8:
        found.append(RegexRule("generic_password", None, SecretType.GENERIC_PASSWORD))
    return found


def is_false_positive(value: str) -> bool:
    """Отметить распространённые публичные и шаблонные значения."""
    stripped = value.strip().strip("\"'")
    if len(stripped) > 128:
        return False
    return bool(_UUID.fullmatch(stripped) or _HEX_HASH.fullmatch(stripped) or _TIMESTAMP.fullmatch(stripped) or _PLACEHOLDER.fullmatch(stripped) or stripped.lower() in _PUBLIC)
