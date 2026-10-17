from __future__ import annotations

import sys
import unittest
from collections.abc import AsyncIterator
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(_SRC))

from git_collector.local.diff_parser import parse_diff_stream
from git_collector.types import DiffLineKind, FileChangeKind


SAMPLE = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 keep
-old
+new
+added
\\ No newline at end of file
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index 3333333..0000000
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
diff --git a/old_name.txt b/new_name.txt
similarity index 100%
rename from old_name.txt
rename to new_name.txt
diff --git a/pic.bin b/pic.bin
new file mode 100644
index 0000000..4444444
Binary files /dev/null and b/pic.bin differ
"""


async def _lines(text: str) -> AsyncIterator[bytes]:
    for line in text.splitlines(keepends=True):
        yield line.encode("utf-8")


class DiffParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_hunks_and_line_numbers(self) -> None:
        files = [f async for f in parse_diff_stream(_lines(SAMPLE), max_file_bytes=1_000_000)]
        self.assertEqual(len(files), 4)

        app = files[0]
        self.assertEqual(app.path, "app.py")
        self.assertEqual(app.change_kind, FileChangeKind.MODIFIED)
        self.assertEqual(len(app.hunks), 1)
        hunk = app.hunks[0]
        kinds = [line.kind for line in hunk.lines]
        self.assertEqual(
            kinds,
            [
                DiffLineKind.CONTEXT,
                DiffLineKind.DELETE,
                DiffLineKind.ADD,
                DiffLineKind.ADD,
            ],
        )
        add_lines = [ln for ln in hunk.lines if ln.kind is DiffLineKind.ADD]
        self.assertEqual(add_lines[0].new_lineno, 2)
        self.assertEqual(add_lines[1].new_lineno, 3)
        self.assertEqual(add_lines[0].text, "new")

    async def test_deleted_and_renamed(self) -> None:
        files = [f async for f in parse_diff_stream(_lines(SAMPLE), max_file_bytes=1_000_000)]
        gone = files[1]
        self.assertEqual(gone.path, "gone.txt")
        self.assertEqual(gone.change_kind, FileChangeKind.DELETED)
        self.assertEqual(gone.hunks[0].lines[0].kind, DiffLineKind.DELETE)

        renamed = files[2]
        self.assertEqual(renamed.change_kind, FileChangeKind.RENAMED)
        self.assertEqual(renamed.path, "new_name.txt")
        self.assertEqual(renamed.old_path, "old_name.txt")

    async def test_binary_skipped(self) -> None:
        files = [f async for f in parse_diff_stream(_lines(SAMPLE), max_file_bytes=1_000_000)]
        binary = files[3]
        self.assertTrue(binary.is_binary)
        self.assertTrue(binary.skipped)
        self.assertEqual(binary.skip_reason, "binary file")
        self.assertEqual(binary.hunks, ())

    async def test_max_file_bytes_skips_rest_of_file(self) -> None:
        big_line = "+" + ("x" * 200)
        tiny = f"""\
diff --git a/big.txt b/big.txt
--- a/big.txt
+++ b/big.txt
@@ -0,0 +1,1 @@
{big_line}
diff --git a/small.txt b/small.txt
--- a/small.txt
+++ b/small.txt
@@ -0,0 +1 @@
+ok
"""
        files = [f async for f in parse_diff_stream(_lines(tiny), max_file_bytes=80)]
        self.assertEqual(len(files), 2)
        self.assertTrue(files[0].skipped)
        self.assertIn("max_file_bytes", files[0].skip_reason or "")
        self.assertFalse(files[1].skipped)
        self.assertEqual(files[1].hunks[0].lines[0].text, "ok")


if __name__ == "__main__":
    unittest.main()
