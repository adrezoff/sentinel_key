"""Git collector: history and diffs behind a swappable ``GitCollector`` backend.

See ``docs/git_collector.md``.
"""

from .config import GitCollectorConfig
from .errors import DiffParseError, GitCollectorError
from .factory import SourceKind, create_git_collector, infer_source_kind
from .github import GitHubCollector
from .gitlab import GitLabCollector
from .local import LocalGitCollector
from .protocol import GitCollector
from .remote import RemoteGitCollector
from .remote_security import normalize_remote_url
from .types import (
    CommitInfo,
    DiffHunk,
    DiffLine,
    DiffLineKind,
    FileChangeKind,
    FileDiff,
)

__all__ = [
    "CommitInfo",
    "DiffHunk",
    "DiffLine",
    "DiffLineKind",
    "DiffParseError",
    "FileChangeKind",
    "FileDiff",
    "GitCollector",
    "GitCollectorConfig",
    "GitCollectorError",
    "GitHubCollector",
    "GitLabCollector",
    "LocalGitCollector",
    "RemoteGitCollector",
    "normalize_remote_url",
    "SourceKind",
    "create_git_collector",
    "infer_source_kind",
]
