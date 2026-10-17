"""Incremental unified-diff parser for ``git diff-tree -p`` output."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from ..types import DiffHunk, DiffLine, DiffLineKind, FileChangeKind, FileDiff

_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")


class _FileBuilder:
    def __init__(self, path: str, old_path: str | None, change_kind: FileChangeKind) -> None:
        self.path = path
        self.old_path = old_path
        self.change_kind = change_kind
        self.is_binary = False
        self.skip_reason: str | None = None
        self._hunks: list[DiffHunk] = []
        self._raw_bytes = 0
        self._current: _HunkBuilder | None = None

    def note_bytes(self, n: int, *, max_file_bytes: int) -> None:
        self._raw_bytes += n
        if self.skip_reason is None and self._raw_bytes > max_file_bytes:
            self.skip_reason = f"file exceeds max_file_bytes={max_file_bytes}"
            self.finish_hunk()

    def finish_hunk(self) -> None:
        if self._current is not None:
            self._hunks.append(self._current.build())
            self._current = None

    def start_hunk(self, old_start: int, old_count: int, new_start: int, new_count: int) -> None:
        self.finish_hunk()
        self._current = _HunkBuilder(old_start, old_count, new_start, new_count)

    def add_line(self, kind: DiffLineKind, text: str) -> None:
        if self._current is None:
            return
        self._current.add(kind, text)

    def build(self) -> FileDiff:
        self.finish_hunk()
        hunks: tuple[DiffHunk, ...] = ()
        if self.skip_reason is None and not self.is_binary:
            hunks = tuple(self._hunks)
        return FileDiff(
            path=self.path,
            change_kind=self.change_kind,
            old_path=self.old_path if self.old_path != self.path else None,
            is_binary=self.is_binary,
            skip_reason=self.skip_reason,
            hunks=hunks,
        )


class _HunkBuilder:
    def __init__(self, old_start: int, old_count: int, new_start: int, new_count: int) -> None:
        self.old_start = old_start
        self.old_count = old_count
        self.new_start = new_start
        self.new_count = new_count
        self._old = old_start
        self._new = new_start
        self._lines: list[DiffLine] = []

    def add(self, kind: DiffLineKind, text: str) -> None:
        old_ln: int | None = None
        new_ln: int | None = None
        if kind is DiffLineKind.CONTEXT:
            old_ln = self._old
            new_ln = self._new
            self._old += 1
            self._new += 1
        elif kind is DiffLineKind.DELETE:
            old_ln = self._old
            self._old += 1
        else:
            new_ln = self._new
            self._new += 1
        self._lines.append(
            DiffLine(kind=kind, text=text, old_lineno=old_ln, new_lineno=new_ln)
        )

    def build(self) -> DiffHunk:
        return DiffHunk(
            old_start=self.old_start,
            old_count=self.old_count,
            new_start=self.new_start,
            new_count=self.new_count,
            lines=tuple(self._lines),
        )


async def parse_diff_stream(
    lines: AsyncIterator[bytes],
    *,
    max_file_bytes: int,
) -> AsyncIterator[FileDiff]:
    current: _FileBuilder | None = None

    async for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")

        if line.startswith("diff --git "):
            if current is not None:
                yield current.build()
            current = _start_file(line)
            continue

        if current is None:
            continue

        current.note_bytes(len(raw), max_file_bytes=max_file_bytes)
        if current.skip_reason is not None:
            # Consume remaining lines until the next ``diff --git`` header.
            continue

        if "Binary files " in line and " differ" in line:
            current.is_binary = True
            current.skip_reason = "binary file"
            current.finish_hunk()
            continue

        if line.startswith("new file mode"):
            current.change_kind = FileChangeKind.ADDED
            continue
        if line.startswith("deleted file mode"):
            current.change_kind = FileChangeKind.DELETED
            continue
        if line.startswith("rename from "):
            current.change_kind = FileChangeKind.RENAMED
            current.old_path = line[len("rename from ") :]
            continue
        if line.startswith("rename to "):
            current.change_kind = FileChangeKind.RENAMED
            current.path = line[len("rename to ") :]
            continue
        if line.startswith("copy from "):
            current.change_kind = FileChangeKind.COPIED
            current.old_path = line[len("copy from ") :]
            continue
        if line.startswith("copy to "):
            current.change_kind = FileChangeKind.COPIED
            current.path = line[len("copy to ") :]
            continue

        hunk = _HUNK_RE.match(line)
        if hunk:
            current.start_hunk(
                int(hunk.group("old_start")),
                int(hunk.group("old_count") or "1"),
                int(hunk.group("new_start")),
                int(hunk.group("new_count") or "1"),
            )
            continue

        if not line:
            continue
        prefix = line[0]
        body = line[1:] if prefix in " +-" else line
        if prefix == "+":
            current.add_line(DiffLineKind.ADD, body)
        elif prefix == "-":
            current.add_line(DiffLineKind.DELETE, body)
        elif prefix == " ":
            current.add_line(DiffLineKind.CONTEXT, body)
        elif line.startswith("\\ "):
            continue

    if current is not None:
        yield current.build()


def _start_file(header: str) -> _FileBuilder:
    match = _DIFF_GIT_RE.match(header)
    if match:
        old_path, new_path = match.group(1), match.group(2)
    else:
        old_path = new_path = header.removeprefix("diff --git ").strip()
    kind = FileChangeKind.MODIFIED
    if old_path == "/dev/null":
        kind = FileChangeKind.ADDED
    elif new_path == "/dev/null":
        kind = FileChangeKind.DELETED
    path = new_path if new_path != "/dev/null" else old_path
    old = old_path if old_path not in (path, "/dev/null") else None
    return _FileBuilder(path=path, old_path=old, change_kind=kind)
