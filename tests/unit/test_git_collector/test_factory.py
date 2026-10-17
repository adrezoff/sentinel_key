from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src"
sys.path.insert(0, str(_SRC))

from git_collector import (
    GitHubCollector,
    GitLabCollector,
    LocalGitCollector,
    SourceKind,
    create_git_collector,
    infer_source_kind,
)


class InferSourceKindTests(unittest.TestCase):
    def test_github_https(self) -> None:
        self.assertEqual(
            infer_source_kind("https://github.com/org/repo.git"),
            SourceKind.GITHUB,
        )

    def test_github_scp(self) -> None:
        self.assertEqual(
            infer_source_kind("git@github.com:org/repo.git"),
            SourceKind.GITHUB,
        )

    def test_github_short(self) -> None:
        self.assertEqual(infer_source_kind("github.com/org/repo"), SourceKind.GITHUB)

    def test_gitlab_https(self) -> None:
        self.assertEqual(
            infer_source_kind("https://gitlab.com/org/repo"),
            SourceKind.GITLAB,
        )

    def test_gitlab_self_hosted(self) -> None:
        self.assertEqual(
            infer_source_kind("https://gitlab.example.com/group/proj"),
            SourceKind.GITLAB,
        )

    def test_existing_local_path(self) -> None:
        self.assertEqual(infer_source_kind(str(Path.cwd())), SourceKind.LOCAL)


class FactoryTests(unittest.TestCase):
    def test_explicit_github(self) -> None:
        collector = create_git_collector("ignored", kind=SourceKind.GITHUB)
        self.assertIsInstance(collector, GitHubCollector)

    def test_explicit_gitlab(self) -> None:
        collector = create_git_collector("ignored", kind=SourceKind.GITLAB)
        self.assertIsInstance(collector, GitLabCollector)

    def test_local_requires_existing_path(self) -> None:
        collector = create_git_collector(str(Path.cwd()), kind=SourceKind.LOCAL)
        self.assertIsInstance(collector, LocalGitCollector)


if __name__ == "__main__":
    unittest.main()
