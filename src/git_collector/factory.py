"""Choose a collector backend from location / explicit ``SourceKind``."""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from urllib.parse import urlparse

from .config import GitCollectorConfig
from .github import GitHubCollector
from .gitlab import GitLabCollector
from .local import LocalGitCollector
from .protocol import GitCollector


class SourceKind(IntEnum):
    LOCAL = 1
    GITHUB = 2
    GITLAB = 3


def infer_source_kind(location: str) -> SourceKind:
    """Heuristic for URLs and local paths. Prefer explicit ``kind`` at call sites."""
    text = location.strip()
    lowered = text.lower()

    if lowered.startswith(("github.com/", "www.github.com/")):
        return SourceKind.GITHUB

    parsed = urlparse(text if "://" in text else f"ssh://{text}")
    host = (parsed.hostname or "").lower()
    if host in {"github.com", "www.github.com"} or host.endswith(".github.com"):
        return SourceKind.GITHUB
    if host in {"gitlab.com", "www.gitlab.com"} or "gitlab." in host:
        return SourceKind.GITLAB

    # scp-like: git@github.com:org/repo.git
    if text.startswith("git@"):
        _, _, rest = text.partition("@")
        host_part, _, _ = rest.partition(":")
        host_part = host_part.lower()
        if host_part in {"github.com", "www.github.com"}:
            return SourceKind.GITHUB
        if host_part == "gitlab.com" or "gitlab." in host_part:
            return SourceKind.GITLAB

    path = Path(text).expanduser()
    if path.exists():
        return SourceKind.LOCAL

    return SourceKind.LOCAL


def create_git_collector(
    location: str,
    *,
    kind: SourceKind | None = None,
    config: GitCollectorConfig | None = None,
    mirror_root: str | Path | None = None,
) -> GitCollector:
    """Build a collector. Remote kinds are stubs until mirror/auth is implemented."""
    cfg = config or GitCollectorConfig()
    resolved = kind if kind is not None else infer_source_kind(location)
    if resolved is SourceKind.LOCAL:
        return LocalGitCollector(location, config=cfg)
    if resolved is SourceKind.GITHUB:
        return GitHubCollector(location, config=cfg, mirror_root=mirror_root)
    if resolved is SourceKind.GITLAB:
        return GitLabCollector(location, config=cfg, mirror_root=mirror_root)
    raise ValueError(f"unsupported source kind: {resolved!r}")
