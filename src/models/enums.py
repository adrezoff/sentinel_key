"""Shared enumerations used by several models (``docs/models.md`` §2)."""

from enum import IntEnum, IntFlag


class SecretType(IntEnum):
    UNKNOWN = 0
    AWS_ACCESS_KEY = 1
    AWS_SECRET_KEY = 2
    GITHUB_TOKEN = 3
    SLACK_TOKEN = 4
    JWT = 5
    PRIVATE_KEY = 6
    BEARER_TOKEN = 7
    DATABASE_URL = 8
    DATABASE_PASSWORD = 9
    API_KEY = 10
    GENERIC_PASSWORD = 11
    OTHER = 99


class Severity(IntEnum):
    """Order of values is meaningful (comparisons, one-level bump)."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class DetectorFlags(IntFlag):
    """Bit mask of which detectors fired. Stored as one SMALLINT column."""

    REGEX = 1
    ENTROPY = 2
    CONTEXT = 4
    LLM = 8
