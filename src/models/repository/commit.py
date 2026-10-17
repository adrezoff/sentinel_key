"""``Commit`` — processing state keyed by SHA (table ``commits``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from ..task.task_error import ErrorCode


class CommitStatus(IntEnum):
    PENDING = 1
    PROCESSED = 2
    FAILED = 3


_SHA_LENGTHS = frozenset({20, 32})


@dataclass(slots=True)
class Commit:
    repository_id: int
    sha: bytes
    id: int | None = None
    status: CommitStatus = CommitStatus.PENDING
    is_merge: bool = False
    committed_at: datetime | None = None
    last_task_id: int | None = None
    scan_run_id: int | None = None
    processed_at: datetime | None = None
    error_code: ErrorCode | None = None

    def __post_init__(self) -> None:
        if len(self.sha) not in _SHA_LENGTHS:
            raise ValueError(f"sha must be 20 or 32 bytes, got {len(self.sha)}")

    def mark_processed(self, *, now: datetime, task_id: int, scan_run_id: int) -> None:
        self.status = CommitStatus.PROCESSED
        self.processed_at = now
        self.last_task_id = task_id
        self.scan_run_id = scan_run_id
        self.error_code = None

    def mark_failed(self, *, error_code: ErrorCode, task_id: int, scan_run_id: int) -> None:
        self.status = CommitStatus.FAILED
        self.error_code = error_code
        self.last_task_id = task_id
        self.scan_run_id = scan_run_id
