from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(_SRC))

from git_collector import GitHubCollector, GitLabCollector


class RemoteStubTests(unittest.IsolatedAsyncioTestCase):
    async def test_github_not_implemented(self) -> None:
        collector = GitHubCollector("https://github.com/org/repo")
        with self.assertRaises(NotImplementedError):
            await collector.list_commits()
        with self.assertRaises(NotImplementedError):
            collector.get_diff(b"\x00" * 20)
        with self.assertRaises(NotImplementedError):
            await collector.file_contains_in_head("x", lambda _: True)

    async def test_gitlab_not_implemented(self) -> None:
        collector = GitLabCollector("https://gitlab.com/org/repo")
        with self.assertRaises(NotImplementedError):
            await collector.list_commits()


if __name__ == "__main__":
    unittest.main()
