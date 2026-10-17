"""GitHub-backed collector (mirror + local git). Not implemented yet.

See ``docs/git_collector.md`` — auth/clone/fetch belong here; diff/history
delegate to ``LocalGitCollector`` once a mirror exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import NoReturn

from .config import GitCollectorConfig
from .types import CommitInfo, FileDiff


class GitHubCollector:
    """Placeholder for GitHub remote → local mirror → ``LocalGitCollector``."""

    def __init__(
        self,
        location: str,
        *,
        config: GitCollectorConfig | None = None,
        mirror_root: str | Path | None = None,
    ) -> None:
        self.location = location
        self.config = config or GitCollectorConfig()
        self.mirror_root = Path(mirror_root) if mirror_root is not None else None

    async def list_commits(self, refs: Sequence[str] | None = None) -> list[CommitInfo]:
        _not_implemented("list_commits")

    def get_diff(self, sha: bytes) -> AsyncIterator[FileDiff]:
        _not_implemented("get_diff")

    async def file_contains_in_head(
        self,
        path: str,
        line_matcher: Callable[[str], bool],
        *,
        ref: str | None = None,
    ) -> bool:
        _not_implemented("file_contains_in_head")


def _not_implemented(method: str) -> NoReturn:
    raise NotImplementedError(
        f"GitHubCollector.{method}: implement ensure_mirror() then delegate to "
        "LocalGitCollector (see docs/git_collector.md)"
    )
