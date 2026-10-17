"""DTO for git history and diffs. Not persisted; not domain table models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum


class FileChangeKind(IntEnum):
    ADDED = 1
    MODIFIED = 2
    DELETED = 3
    RENAMED = 4
    COPIED = 5


class DiffLineKind(IntEnum):
    CONTEXT = 1
    ADD = 2
    DELETE = 3


@dataclass(frozen=True, slots=True)
class CommitInfo:
    """One commit from the VCS graph (planner maps this into ``models.Commit``)."""

    sha: bytes
    parent_shas: tuple[bytes, ...]
    committed_at: datetime | None

    @property
    def is_merge(self) -> bool:
        return len(self.parent_shas) > 1


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: DiffLineKind
    text: str
    old_lineno: int | None
    new_lineno: int | None


@dataclass(frozen=True, slots=True)
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[DiffLine, ...] = ()


@dataclass(frozen=True, slots=True)
class FileDiff:
    """One file change inside a commit. Empty ``hunks`` when skipped or binary."""

    path: str
    change_kind: FileChangeKind
    old_path: str | None = None
    is_binary: bool = False
    skip_reason: str | None = None
    hunks: tuple[DiffHunk, ...] = field(default_factory=tuple)

    @property
    def skipped(self) -> bool:
        return self.skip_reason is not None
