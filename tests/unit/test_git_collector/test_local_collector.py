from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(_SRC))

from git_collector import GitCollectorConfig, LocalGitCollector
from git_collector.errors import GitCollectorError
from git_collector.local.collector import _diff_tree_args, _parse_log
from git_collector.types import DiffLineKind, FileChangeKind


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    # Deterministic default branch name across git versions.
    _git(root, "checkout", "-b", "main")


class ParseLogTests(unittest.TestCase):
    def test_parse_log_records(self) -> None:
        sha = "a" * 40
        parent = "b" * 40
        blob = f"{sha}\x00{parent}\x001700000000\n".encode()
        commits = _parse_log(blob)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].sha, bytes.fromhex(sha))
        self.assertEqual(commits[0].parent_shas, (bytes.fromhex(parent),))
        self.assertFalse(commits[0].is_merge)
        self.assertIsNotNone(commits[0].committed_at)

    def test_root_commit_no_parents(self) -> None:
        sha = "c" * 40
        commits = _parse_log(f"{sha}\x00\x001700000000\n".encode())
        self.assertEqual(commits[0].parent_shas, ())


class DiffTreeArgsTests(unittest.TestCase):
    def test_root_commit(self) -> None:
        args = _diff_tree_args("deadbeef", [])
        self.assertIn("--root", args)
        self.assertEqual(args[-1], "deadbeef")

    def test_merge_uses_first_parent_only(self) -> None:
        args = _diff_tree_args("merge", ["parent1", "parent2"])
        self.assertEqual(args[-2:], ["parent1", "merge"])
        self.assertNotIn("parent2", args)


class LocalGitCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmpdir.name)
        _init_repo(self.repo)

        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        _git(self.repo, "add", "a.txt")
        _git(self.repo, "commit", "-m", "root")

        _git(self.repo, "checkout", "-b", "feature")
        (self.repo / "a.txt").write_text("one\nfeature-secret\n", encoding="utf-8")
        _git(self.repo, "add", "a.txt")
        _git(self.repo, "commit", "-m", "feature change")

        _git(self.repo, "checkout", "main")
        (self.repo / "b.txt").write_text("main-only\n", encoding="utf-8")
        _git(self.repo, "add", "b.txt")
        _git(self.repo, "commit", "-m", "main change")

        _git(self.repo, "merge", "feature", "-m", "merge feature")
        self.collector = LocalGitCollector(self.repo, config=GitCollectorConfig())

    async def asyncTearDown(self) -> None:
        self._tmpdir.cleanup()

    async def test_list_commits_topo_oldest_first(self) -> None:
        commits = await self.collector.list_commits(["main"])
        self.assertGreaterEqual(len(commits), 4)
        self.assertEqual(commits[0].parent_shas, ())
        self.assertTrue(any(c.is_merge for c in commits))
        # Parent of each non-root commit appears earlier in the list.
        index = {c.sha: i for i, c in enumerate(commits)}
        for commit in commits[1:]:
            for parent in commit.parent_shas:
                if parent in index:
                    self.assertLess(index[parent], index[commit.sha])

    async def test_merge_diff_against_first_parent(self) -> None:
        commits = await self.collector.list_commits(["main"])
        merge = next(c for c in commits if c.is_merge)
        self.assertEqual(len(merge.parent_shas), 2)
        files = [f async for f in self.collector.get_diff(merge.sha)]
        # First-parent diff of merge brings feature's a.txt change onto main.
        app = next(f for f in files if f.path == "a.txt")
        added = [
            ln.text
            for hunk in app.hunks
            for ln in hunk.lines
            if ln.kind is DiffLineKind.ADD
        ]
        self.assertIn("feature-secret", added)

    async def test_get_diff_added_line(self) -> None:
        commits = await self.collector.list_commits(["main"])
        # Second commit on main added b.txt
        main_tip_parents = [c for c in commits if not c.is_merge and c.parent_shas]
        target = main_tip_parents[0]
        # Find commit that introduced b.txt by scanning diffs.
        found = False
        for commit in commits:
            async for file_diff in self.collector.get_diff(commit.sha):
                if file_diff.path == "b.txt" and file_diff.change_kind is FileChangeKind.ADDED:
                    adds = [
                        ln.text
                        for hunk in file_diff.hunks
                        for ln in hunk.lines
                        if ln.kind is DiffLineKind.ADD
                    ]
                    self.assertIn("main-only", adds)
                    found = True
        self.assertTrue(found)

    async def test_file_contains_in_head(self) -> None:
        self.assertTrue(
            await self.collector.file_contains_in_head(
                "a.txt", lambda line: "feature-secret" in line
            )
        )
        self.assertFalse(
            await self.collector.file_contains_in_head(
                "a.txt", lambda line: "missing-token" in line
            )
        )
        self.assertFalse(
            await self.collector.file_contains_in_head(
                "no-such.txt", lambda line: True
            )
        )

    async def test_unsafe_path_rejected(self) -> None:
        with self.assertRaises(GitCollectorError):
            await self.collector.file_contains_in_head("../x", lambda line: True)

    def test_missing_repo_path(self) -> None:
        with self.assertRaises(GitCollectorError):
            LocalGitCollector(self.repo / "does-not-exist")


if __name__ == "__main__":
    unittest.main()
