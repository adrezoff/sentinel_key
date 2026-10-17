"""``Incident`` — подтверждённая или высоковероятная утечка credentials (таблица ``incidents``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum

from ..candidate.candidate import Candidate
from ..common.clock import utc_now
from ..enums import DetectorFlags, SecretType, Severity
from ..exceptions import InvalidTransition
from .incident_event import IncidentEvent, IncidentEventKind
from .occurrence import Occurrence

_FINGERPRINT_SIZE = 32


class IncidentStatus(IntEnum):
    """Жизненный цикл инцидента. Из закрытых статусов возможен только reopen → OPEN."""

    OPEN = 1
    ACKNOWLEDGED = 2
    REMEDIATED = 3
    FALSE_POSITIVE = 4
    ACCEPTED_RISK = 5


TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.OPEN: frozenset(
        {
            IncidentStatus.ACKNOWLEDGED,
            IncidentStatus.REMEDIATED,
            IncidentStatus.FALSE_POSITIVE,
            IncidentStatus.ACCEPTED_RISK,
        }
    ),
    IncidentStatus.ACKNOWLEDGED: frozenset(
        {
            IncidentStatus.REMEDIATED,
            IncidentStatus.FALSE_POSITIVE,
            IncidentStatus.ACCEPTED_RISK,
        }
    ),
    IncidentStatus.REMEDIATED: frozenset({IncidentStatus.OPEN}),
    IncidentStatus.FALSE_POSITIVE: frozenset({IncidentStatus.OPEN}),
    IncidentStatus.ACCEPTED_RISK: frozenset({IncidentStatus.OPEN}),
}

_RESOLVED_STATUSES = frozenset({IncidentStatus.REMEDIATED, IncidentStatus.FALSE_POSITIVE})


@dataclass(slots=True)
class Incident:
    """Инцидент по секрету в репозитории. Дедуп: ``UNIQUE(repository_id, fingerprint)``.

    Статус и severity меняются только через методы модели; каждый вызов даёт ``IncidentEvent``.
    Сырого секрета нет — только ``evidence_masked``.
    """

    repository_id: int
    fingerprint: bytes
    secret_type: SecretType
    severity: Severity
    confidence: float
    detector_flags: DetectorFlags
    evidence_masked: str
    reason: str
    recommendation: str
    first_commit_id: int
    id: int | None = None
    status: IncidentStatus = IncidentStatus.OPEN
    still_in_head: bool | None = None
    assignee: str | None = None
    last_verdict_id: int | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    resolved_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if len(self.fingerprint) != _FINGERPRINT_SIZE:
            raise ValueError(
                f"fingerprint must be {_FINGERPRINT_SIZE} bytes, got {len(self.fingerprint)}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")

    def set_status(
        self,
        new: IncidentStatus,
        actor: str | None,
        note: str | None,
        *,
        now: datetime | None = None,
    ) -> IncidentEvent:
        """Сменить статус по автомату и вернуть событие ``STATUS_CHANGED`` / ``REOPENED``."""
        allowed = TRANSITIONS.get(self.status, frozenset())
        if new not in allowed:
            raise InvalidTransition(self.status, new, entity="Incident")
        when = now if now is not None else utc_now()
        self.status = new
        self.updated_at = when
        self.version += 1
        if new in _RESOLVED_STATUSES:
            self.resolved_at = when
        elif new is IncidentStatus.OPEN:
            self.resolved_at = None
        kind = (
            IncidentEventKind.REOPENED
            if new is IncidentStatus.OPEN
            else IncidentEventKind.STATUS_CHANGED
        )
        return IncidentEvent(
            incident_id=_require_id(self.id),
            kind=kind,
            note=note,
            actor=actor,
            created_at=when,
        )

    def raise_severity(
        self,
        new: Severity,
        *,
        actor: str | None = "system",
        note: str | None = None,
        now: datetime | None = None,
    ) -> IncidentEvent:
        """Повысить критичность (только вверх) и вернуть ``SEVERITY_RAISED``."""
        if new <= self.severity:
            raise ValueError(f"severity can only increase: {self.severity!r} → {new!r}")
        when = now if now is not None else utc_now()
        self.severity = new
        self.updated_at = when
        self.version += 1
        return IncidentEvent(
            incident_id=_require_id(self.id),
            kind=IncidentEventKind.SEVERITY_RAISED,
            note=note,
            actor=actor,
            created_at=when,
        )

    def add_occurrence(
        self,
        candidate: Candidate,
        ai_task_id: int | None,
        *,
        now: datetime | None = None,
    ) -> Occurrence:
        """Добавить обнаружение по уже сохранённому кандидату (нужен ``candidate.id``)."""
        when = now if now is not None else utc_now()
        candidate_id = candidate.id
        if candidate_id is None:
            raise ValueError("candidate.id is required before add_occurrence")
        self.updated_at = when
        self.version += 1
        return Occurrence(
            incident_id=_require_id(self.id),
            candidate_id=candidate_id,
            ai_task_id=ai_task_id,
            seen_at=when,
        )


def _require_id(value: int | None) -> int:
    if value is None:
        raise ValueError("incident.id is required (persist the incident first)")
    return value
