"""``Candidate`` — persisted potential secret (table ``candidates``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum

from ..common.clock import utc_now
from ..enums import DetectorFlags, SecretType
from .score_breakdown import ScoreBreakdown


class Route(IntEnum):
    IGNORE = 0
    SUSPICIOUS = 1
    AI_VALIDATION = 2


_FINGERPRINT_SIZE = 32


@dataclass(slots=True)
class Candidate:
    repository_id: int
    commit_id: int
    file_path: str
    line_number: int
    secret_type: SecretType
    value_masked: str
    value_fingerprint: bytes
    entropy: float
    detector_flags: DetectorFlags
    source_line_masked: str
    context_masked: str
    is_test_context: bool
    score_regex: float
    score_entropy: float
    score_context: float
    score_token_type: float
    score_total: float
    route: Route
    id: int | None = None
    key_name: str | None = None
    regex_rules: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if len(self.value_fingerprint) != _FINGERPRINT_SIZE:
            raise ValueError(
                f"value_fingerprint must be {_FINGERPRINT_SIZE} bytes, "
                f"got {len(self.value_fingerprint)}"
            )

    @property
    def score(self) -> ScoreBreakdown:
        return ScoreBreakdown(
            regex=self.score_regex,
            entropy=self.score_entropy,
            context=self.score_context,
            token_type=self.score_token_type,
        )
