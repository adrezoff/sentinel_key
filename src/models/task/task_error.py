"""``TaskError`` — value object ошибки задачи; ``ErrorCode`` — коды (``docs/models.md`` §3.4.1)."""

from dataclasses import dataclass
from enum import IntEnum


class ErrorCode(IntEnum):
    """Код последней ошибки задачи или коммита. Числовые значения закреплены."""

    GIT_ERROR = 1
    DIFF_ERROR = 2
    LLM_FAILED = 3
    LLM_INVALID_RESPONSE = 4
    MASKING_VIOLATION = 5
    TIMEOUT = 6
    LEASE_EXPIRED = 7
    INTERNAL = 99


@dataclass(frozen=True, slots=True)
class TaskError:
    """Ошибка попытки выполнения задачи: код, безопасное сообщение, флаг retry."""

    code: ErrorCode
    message: str
    retryable: bool
