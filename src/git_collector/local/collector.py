"""``LocalGitCollector`` — history and diffs via the ``git`` CLI."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from ..config import GitCollectorConfig
from ..errors import GitCollectorError
from ..types import CommitInfo, FileDiff
from .diff_parser import parse_diff_stream
from .sha import parse_sha_hex, sha_to_hex
from .subprocess import GitProcess


class LocalGitCollector:
    """Collector bound to an existing local working tree or bare repository."""

    def __init__(self, location: str | Path, *, config: GitCollectorConfig | None = None) -> None:
        self._config = config or GitCollectorConfig()
        self._path = Path(location).expanduser().resolve()
        if not self._path.exists():
            raise GitCollectorError(f"repository path does not exist: {self._path}")
        self._git = GitProcess(str(self._path), self._config)

    @property
    def location(self) -> Path:
        return self._path

    async def list_commits(self, refs: Sequence[str] | None = None) -> list[CommitInfo]:
        ref_args = list(refs) if refs else ["--all"]
        # ``git log --format`` emits one record per line (no ``commit <sha>`` preamble).
        stdout = await self._git.run(
            [
                "log",
                "--topo-order",
                "--reverse",
                "--format=%H%x00%P%x00%ct",
                *ref_args,
            ]
        )
        return _parse_log(stdout)

    def get_diff(self, sha: bytes) -> AsyncIterator[FileDiff]:
        return self._iter_diff(sha)

    async def _iter_diff(self, sha: bytes) -> AsyncIterator[FileDiff]:
        hex_sha = sha_to_hex(sha)
        parents = await self._parent_hexes(hex_sha)
        args = _diff_tree_args(hex_sha, parents)
        lines = self._git.stream_lines(args)
        async for file_diff in parse_diff_stream(
            lines, max_file_bytes=self._config.max_file_bytes
        ):
            yield file_diff

    async def file_contains_in_head(
        self,
        path: str,
        line_matcher: Callable[[str], bool],
        *,
        ref: str | None = None,
    ) -> bool:
        if path.startswith("/") or ".." in Path(path).parts:
            raise GitCollectorError(f"refusing unsafe path: {path!r}")
        spec = f"{ref or 'HEAD'}:{path}"
        try:
            blob = await self._git.run(["show", spec])
        except GitCollectorError as exc:
            err = exc.stderr or ""
            if any(
                token in err
                for token in (
                    "does not exist",
                    "exists on disk",
                    "Not a valid object",
                    "bad object",
                    "path not in",
                )
            ):
                return False
            raise
        text = blob.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line_matcher(line):
                return True
        return False

    async def _parent_hexes(self, hex_sha: str) -> list[str]:
        stdout = await self._git.run(["rev-list", "--parents", "-n", "1", hex_sha])
        parts = stdout.decode("ascii", errors="replace").strip().split()
        if not parts or parts[0].lower() != hex_sha.lower():
            raise GitCollectorError(f"unexpected rev-list --parents output for {hex_sha}")
        return parts[1:]


def _diff_tree_args(hex_sha: str, parent_hexes: list[str]) -> list[str]:
    base = ["diff-tree", "-p", "--find-renames", "--no-commit-id", "--root"]
    if not parent_hexes:
        return [*base, hex_sha]
    # Merge: first parent only (modules.md §3 / docs/git_collector.md).
    return [*base, parent_hexes[0], hex_sha]


def _parse_log(stdout: bytes) -> list[CommitInfo]:
    commits: list[CommitInfo] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.decode("utf-8", errors="replace").split("\x00")
        if len(parts) < 3:
            continue
        sha_hex, parents_raw, ts_raw = parts[0], parts[1], parts[2]
        sha = parse_sha_hex(sha_hex)
        parent_shas = tuple(parse_sha_hex(p) for p in parents_raw.split() if p)
        try:
            committed_at = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
        except ValueError:
            committed_at = None
        commits.append(
            CommitInfo(sha=sha, parent_shas=parent_shas, committed_at=committed_at)
        )
    return commits
