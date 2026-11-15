"""Выбор локального или удалённого коллектора по источнику репозитория."""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from urllib.parse import urlparse

from .config import GitCollectorConfig
from .github import GitHubCollector
from .gitlab import GitLabCollector
from .local import LocalGitCollector
from .protocol import GitCollector
from .remote import RemoteGitCollector


class SourceKind(IntEnum):
    """Тип источника с постоянными числовыми значениями."""

    LOCAL = 1
    GITHUB = 2
    GITLAB = 3
    REMOTE = 4


def infer_source_kind(location: str) -> SourceKind:
    """Определить тип источника; окончательную проверку URL выполняет адаптер."""
    text = location.strip()
    lowered = text.lower()

    if lowered.startswith(("github.com/", "www.github.com/")):
        return SourceKind.GITHUB

    try:
        parsed = urlparse(text if "://" in text else f"ssh://{text}")
        host = (parsed.hostname or "").lower()
    except ValueError:
        # Подробности невалидного URL не должны попасть в ошибки CLI.
        return SourceKind.REMOTE if "://" in text else SourceKind.LOCAL
    if host in {"github.com", "www.github.com"} or host.endswith(".github.com"):
        return SourceKind.GITHUB
    if host in {"gitlab.com", "www.gitlab.com"} or "gitlab." in host:
        return SourceKind.GITLAB

    if "://" in text:
        return SourceKind.REMOTE

    # scp-like: git@github.com:org/repo.git
    if text.startswith("git@"):
        _, _, rest = text.partition("@")
        host_part, _, _ = rest.partition(":")
        host_part = host_part.lower()
        if host_part in {"github.com", "www.github.com"}:
            return SourceKind.GITHUB
        if host_part == "gitlab.com" or "gitlab." in host_part:
            return SourceKind.GITLAB

    return SourceKind.LOCAL


def create_git_collector(
    location: str,
    *,
    kind: SourceKind | None = None,
    config: GitCollectorConfig | None = None,
    mirror_root: str | Path | None = None,
) -> GitCollector:
    """Создать коллектор; конструктор удалённого источника не вызывает сеть."""
    cfg = config or GitCollectorConfig()
    resolved = kind if kind is not None else infer_source_kind(location)
    if resolved is SourceKind.LOCAL:
        return LocalGitCollector(location, config=cfg)
    if resolved is SourceKind.GITHUB:
        return GitHubCollector(location, config=cfg, mirror_root=mirror_root)
    if resolved is SourceKind.GITLAB:
        return GitLabCollector(location, config=cfg, mirror_root=mirror_root)
    if resolved is SourceKind.REMOTE:
        return RemoteGitCollector(location, config=cfg, mirror_root=mirror_root)
    raise ValueError(f"unsupported source kind: {resolved!r}")
