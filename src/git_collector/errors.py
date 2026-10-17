"""Collector errors mapped to domain ``ErrorCode`` for workers / commit status."""

from __future__ import annotations

from models.task import ErrorCode


class GitCollectorError(Exception):
    """Raised when a collector operation fails (repo missing, bad SHA, git exit ≠ 0)."""

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.GIT_ERROR,
        stderr: str | None = None,
    ) -> None:
        self.code = code
        self.stderr = stderr
        super().__init__(message)


class DiffParseError(GitCollectorError):
    """Single-file / stream parse failure → skip file with ``DIFF_ERROR`` upstream."""

    def __init__(self, message: str, *, stderr: str | None = None) -> None:
        super().__init__(message, code=ErrorCode.DIFF_ERROR, stderr=stderr)
