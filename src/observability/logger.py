"""Структурное JSON-логирование без секретов.

Правила (AGENTS.md §6.1, §6.2, modules.md §13):
- НИКАКИХ сырых секретов, value_fingerprint, source_line_raw, context_raw
- Только маскированные поля: value_masked, source_line_masked, context_masked
- JSON-формат с обязательными полями: task_id, run_id, commit_id, candidate_id
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Глобальный сборщик логов для тестов
_log_buffer: list[dict] = []
# Глобальный сборщик логов для тестов
_log_buffer: list[dict] = []


@dataclass(slots=True)
class LogContext:
    """Контекст для обогащения логов."""
    task_id: Optional[int] = None
    run_id: Optional[int] = None
    commit_id: Optional[int] = None
    candidate_id: Optional[int] = None


class StructuredFormatter(logging.Formatter):
    """JSON-форматтер с обязательными полями и санитизацией."""

    def __init__(self) -> None:
        super().__init__()
        self.default_fields = {
            "service": "sentinel_key",
            "version": "1.0.0",
        }

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **self.default_fields,
        }

        for key in ("task_id", "run_id", "commit_id", "candidate_id"):
            value = getattr(record, key, None)
            if value is not None:
                log_data[key] = value

        for key, value in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "lineno", "funcName", "created",
                "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process", "exc_info", "exc_text", "stack_info",
                "task_id", "run_id", "commit_id", "candidate_id", "message",
            }:
                if isinstance(value, str) and self._looks_like_secret(value):
                    log_data[key] = "***MASKED***"
                else:
                    log_data[key] = value

        return json.dumps(log_data, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _looks_like_secret(value: str) -> bool:
        if len(value) < 16:
            return False
        import re
        patterns = [
            r"^AKIA[A-Z0-9]{16}$",
            r"^ghp_[A-Za-z0-9_]{36,}$",
            r"^xox[baprs]-[A-Za-z0-9-]{10,}$",
            r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$",
        ]
        return any(re.match(p, value) for p in patterns)


class SecretSafeLogger(logging.Logger):
    """Логгер, гарантирующий отсутствие секретов в выводе."""

    def _log(self, level: int, msg: object, args: tuple, exc_info=None, extra=None, stack_info: bool = False, stacklevel: int = 1) -> None:
        if isinstance(msg, str):
            msg = self._sanitize_message(msg)
        super()._log(level, msg, args, exc_info, extra, stack_info, stacklevel + 1)

    @staticmethod
    def _sanitize_message(msg: str) -> str:
        import re
        patterns = [
            (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "AKIA***MASKED***"),
            (re.compile(r"\bghp_[A-Za-z0-9_]{36,}\b"), "ghp_***MASKED***"),
            (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "xox***MASKED***"),
            (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "JWT.***MASKED***"),
        ]
        for pattern, replacement in patterns:
            msg = pattern.sub(replacement, msg)
        return msg


class ContextLogger:
    """Обёртка для логирования с автоматическим контекстом."""

    def __init__(self, logger: logging.Logger, context: LogContext = None):
        self._logger = logger
        self._context = context or LogContext()

    def with_context(self, **kwargs) -> "ContextLogger":
        new_context = LogContext(
            task_id=kwargs.get("task_id", self._context.task_id),
            run_id=kwargs.get("run_id", self._context.run_id),
            commit_id=kwargs.get("commit_id", self._context.commit_id),
            candidate_id=kwargs.get("candidate_id", self._context.candidate_id),
        )
        return ContextLogger(self._logger, new_context)

    def _log(self, level: int, msg: str, **kwargs) -> None:
        extra = {
            "task_id": self._context.task_id,
            "run_id": self._context.run_id,
            "commit_id": self._context.commit_id,
            "candidate_id": self._context.candidate_id,
            **kwargs,
        }
        self._logger.log(level, msg, extra=extra)

    def debug(self, msg: str, **kwargs) -> None:
        self._log(logging.DEBUG, msg, **kwargs)

    def info(self, msg: str, **kwargs) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def critical(self, msg: str, **kwargs) -> None:
        self._log(logging.CRITICAL, msg, **kwargs)

    def exception(self, msg: str, **kwargs) -> None:
        self._log(logging.ERROR, msg, exc_info=True, **kwargs)


# Глобальный логгер
_root_logger: logging.Logger | None = None
_context_logger: ContextLogger | None = None


def configure_logging(
    level: int = logging.INFO,
    json_output: bool = True,
    stream: Any = sys.stdout,
) -> None:
    """Настраивает глобальное логирование."""
    global _root_logger, _context_logger

    logging.setLoggerClass(SecretSafeLogger)
    _root_logger = logging.getLogger("sentinel_key")
    _root_logger.setLevel(level)
    _root_logger.handlers.clear()

    handler = logging.StreamHandler(stream)
    handler.setLevel(level)

    if json_output:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        ))

    _root_logger.addHandler(handler)
    _root_logger.propagate = False

    _context_logger = ContextLogger(_root_logger)


def get_logger(name: str = "sentinel_key", context: LogContext = None) -> ContextLogger:
    """Возвращает логгер с контекстом."""
    global _context_logger
    if _context_logger is None:
        configure_logging()
    if context is not None:
        return ContextLogger(_root_logger, context)
    return _context_logger


def get_test_logs() -> list[dict]:
    """Возвращает буфер логов для тестов."""
    return _log_buffer


def clear_test_logs() -> None:
    """Очищает буфер тестовых логов."""
    _log_buffer.clear()