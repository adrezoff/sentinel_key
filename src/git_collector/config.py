"""Неизменяемые настройки чтения Git и ограничения сетевой нагрузки."""

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class GitCollectorConfig:
    """Лимиты локальных процессов и обновлений удалённого кэша."""

    max_file_bytes: int = 1_048_576
    git_workers: int = 4
    command_timeout_s: float = 120.0
    git_executable: str = "git"
    remote_cache_ttl_s: float = 300.0
    remote_host_interval_s: float = 60.0
    remote_failure_cooldown_s: float = 900.0
    remote_timeout_s: float = 300.0
    allow_http: bool = False

    def __post_init__(self) -> None:
        for name in (
            "command_timeout_s", "remote_cache_ttl_s", "remote_host_interval_s",
            "remote_failure_cooldown_s", "remote_timeout_s",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.git_workers < 1 or self.max_file_bytes < 1:
            raise ValueError("git_workers and max_file_bytes must be positive")
        if self.remote_failure_cooldown_s < self.remote_host_interval_s:
            raise ValueError("failure cooldown must be at least the host interval")
