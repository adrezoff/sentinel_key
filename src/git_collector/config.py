"""Frozen settings for collectors. Thresholds stay out of call sites."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GitCollectorConfig:
    """Defaults aligned with ``docs/modules.md`` §2."""

    max_file_bytes: int = 1_048_576
    git_workers: int = 4
    command_timeout_s: float = 120.0
    git_executable: str = "git"
