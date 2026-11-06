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

from security.redaction import contains_secret, redact_text


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
            "message": redact_text(record.getMessage()),
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
                if isinstance(value, str):
                    log_data[key] = "***MASKED***" if contains_secret(value) else value
                else:
                    log_data[key] = value

        return json.dumps(log_data, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _looks_like_secret(value: str) -> bool:
        """Проверить наличие поддерживаемого секрета в строке."""
        return contains_secret(value)


class SecretSafeLogger(logging.Logger):
    """Логгер, гарантирующий отсутствие секретов в выводе."""

    def _log(self, level: int, msg: object, args: tuple, exc_info=None, extra=None, stack_info: bool = False, stacklevel: int = 1) -> None:
        if isinstance(msg, str):
            msg = self._sanitize_message(msg)
        if extra:
            extra = {
                key: self._sanitize_message(value) if isinstance(value, str) else value
                for key, value in extra.items()
            }
        super()._log(level, msg, args, exc_info, extra, stack_info, stacklevel + 1)

    @staticmethod
    def _sanitize_message(msg: str) -> str:
        """Удалить поддерживаемые секреты из произвольного текста сообщения."""
        return redact_text(msg)


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
        self._log(logging.ERROR, msg, **kwargs)

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