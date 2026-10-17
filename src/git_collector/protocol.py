"""Backend-agnostic collector contract (local git, GitHub, GitLab, …)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Protocol, runtime_checkable

from .types import CommitInfo, FileDiff


@runtime_checkable
class GitCollector(Protocol):
    """Read-only view of a repository history and diffs.

    Implementations must not analyse content for secrets. Binding to a concrete
    repository happens in the constructor (``location`` / mirror path).
    """

    async def list_commits(self, refs: Sequence[str] | None = None) -> list[CommitInfo]:
        """Commits reachable from ``refs`` (default ``--all``), oldest → newest."""

    def get_diff(self, sha: bytes) -> AsyncIterator[FileDiff]:
        """Return an async iterator of per-file diffs (do not ``await`` the call).

        Merge commits: first parent only.
        """

    async def file_contains_in_head(
        self,
        path: str,
        line_matcher: Callable[[str], bool],
        *,
        ref: str | None = None,
    ) -> bool:
        """True if any line of ``path`` at ``ref`` (default HEAD) matches."""
