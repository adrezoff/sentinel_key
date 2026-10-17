"""``CommitParent`` — git parent edges (table ``commit_parents``)."""

from dataclasses import dataclass

_SHA_LENGTHS = frozenset({20, 32})


@dataclass(slots=True)
class CommitParent:
    commit_id: int
    position: int
    parent_sha: bytes

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("position must be >= 0 (0 = first parent)")
        if len(self.parent_sha) not in _SHA_LENGTHS:
            raise ValueError(f"parent_sha must be 20 or 32 bytes, got {len(self.parent_sha)}")
