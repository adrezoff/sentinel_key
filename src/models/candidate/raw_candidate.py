"""``RawCandidate`` — только в памяти. Единственный класс, которому разрешено держать сырой секрет."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NoReturn

from ..common.clock import utc_now
from ..enums import DetectorFlags, SecretType
from .candidate import Candidate, Route
from .score_breakdown import ScoreBreakdown

_FINGERPRINT_SIZE = 32


def _refuse_serialization(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise TypeError("RawCandidate cannot be pickled or serialized")


@dataclass(slots=True)
class RawCandidate:
    """Кандидат до маскирования и scoring: живёт только в процессе.

    ``raw_value`` / ``source_line_raw`` / ``context_raw`` не пишутся в БД, логи и ``repr``.
    Сериализация и копирование запрещены.
    """

    repository_id: int
    commit_id: int
    file_path: str
    line_number: int
    raw_value: str = field(repr=False)
    source_line_raw: str = field(repr=False)
    context_raw: list[str] = field(repr=False)
    source_line_masked: str
    context_masked: str
    secret_type_hint: SecretType
    value_fingerprint: bytes
    value_masked: str
    key_name: str | None = None

    def __post_init__(self) -> None:
        if len(self.value_fingerprint) != _FINGERPRINT_SIZE:
            raise ValueError(
                f"value_fingerprint must be {_FINGERPRINT_SIZE} bytes, "
                f"got {len(self.value_fingerprint)}"
            )

    def __repr__(self) -> str:
        return (
            f"RawCandidate(repository_id={self.repository_id}, commit_id={self.commit_id}, "
            f"file_path={self.file_path!r}, line_number={self.line_number}, "
            f"key_name={self.key_name!r}, secret_type_hint={self.secret_type_hint!r}, "
            f"value_masked={self.value_masked!r})"
        )

    def to_candidate(
        self,
        score: ScoreBreakdown,
        flags: DetectorFlags,
        route: Route,
        *,
        entropy: float,
        regex_rules: list[str],
        is_test_context: bool,
        secret_type: SecretType | None = None,
        created_at: datetime | None = None,
    ) -> Candidate:
        """Собрать сохраняемый ``Candidate`` без копирования сырых полей."""
        return Candidate(
            repository_id=self.repository_id,
            commit_id=self.commit_id,
            file_path=self.file_path,
            line_number=self.line_number,
            key_name=self.key_name,
            secret_type=self.secret_type_hint if secret_type is None else secret_type,
            value_masked=self.value_masked,
            value_fingerprint=self.value_fingerprint,
            entropy=entropy,
            regex_rules=list(regex_rules),
            detector_flags=flags,
            source_line_masked=self.source_line_masked,
            context_masked=self.context_masked,
            is_test_context=is_test_context,
            score_regex=score.regex,
            score_entropy=score.entropy,
            score_context=score.context,
            score_token_type=score.token_type,
            score_total=score.total,
            route=route,
            created_at=created_at if created_at is not None else utc_now(),
        )


RawCandidate.__reduce__ = _refuse_serialization  # type: ignore[method-assign]
RawCandidate.__reduce_ex__ = _refuse_serialization  # type: ignore[method-assign]
RawCandidate.__getstate__ = _refuse_serialization  # type: ignore[method-assign]
RawCandidate.__copy__ = _refuse_serialization  # type: ignore[method-assign]
RawCandidate.__deepcopy__ = _refuse_serialization  # type: ignore[method-assign]
