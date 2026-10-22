"""Конфигурация предобработки diff без чувствительных данных."""

from dataclasses import dataclass, field


def _default_ignored_path_parts() -> frozenset[str]:
    return frozenset({"vendor", "node_modules", "dist", "build", ".git"})


def _default_ignored_file_names() -> frozenset[str]:
    return frozenset(
        {
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "poetry.lock",
            "Pipfile.lock",
            "Cargo.lock",
        }
    )


@dataclass(frozen=True, slots=True)
class PreprocessorConfig:
    """Лимиты и фильтры чистого преобразования ``FileDiff`` в ``RawCandidate``."""

    context_lines: int = 2
    max_line_len: int = 4096
    min_bare_token_len: int = 20
    mask_visible_chars: int = 4
    ignored_path_parts: frozenset[str] = field(default_factory=_default_ignored_path_parts)
    ignored_file_names: frozenset[str] = field(default_factory=_default_ignored_file_names)
    ignored_suffixes: tuple[str, ...] = (".lock", ".min.js", ".min.css")

    def __post_init__(self) -> None:
        if self.context_lines < 0:
            raise ValueError("context_lines must be non-negative")
        if self.max_line_len <= 0:
            raise ValueError("max_line_len must be positive")
        if self.min_bare_token_len <= 0:
            raise ValueError("min_bare_token_len must be positive")
        if self.mask_visible_chars < 0:
            raise ValueError("mask_visible_chars must be non-negative")
