"""Атомарное применение локальной политики инцидентов к сохранённым кандидатам."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from config import IncidentPolicy
from models import Candidate, Incident, IncidentEvent, IncidentEventKind, IncidentStatus, Occurrence, Severity
from models.common.clock import utc_now


class IncidentSession(Protocol):
    """Нужные менеджеру операции транзакционной сессии хранилища."""

    def get_incident_by_fingerprint(self, repository_id: int, fingerprint: bytes) -> Incident | None:
        """Найти инцидент по естественному ключу."""
        ...

    def save_incident(self, incident: Incident, *, expected_version: int | None = None) -> Incident:
        """Создать либо обновить инцидент с проверкой версии."""
        ...

    def has_occurrence(self, incident_id: int, candidate_id: int) -> bool:
        """Проверить наличие обнаружения."""
        ...

    def save_occurrence(self, occurrence: Occurrence) -> None:
        """Сохранить обнаружение идемпотентно."""
        ...

    def save_incident_event(self, event: IncidentEvent) -> IncidentEvent:
        """Добавить запись в журнал инцидента."""
        ...


_RECOMMENDATION = (
    "Rotate or revoke the credential, then remove it from Git history. "
    "Deleting it from the latest commit does not revoke it."
)


class IncidentManager:
    """Решает об инциденте и обновляет его граф внутри общей транзакции."""

    def __init__(self, policy: IncidentPolicy | None = None) -> None:
        """Получить политику явно или использовать документированные значения."""
        self.policy = policy if policy is not None else IncidentPolicy()

    def decide(self, candidate: Candidate) -> bool:
        """Проверить локальное правило без I/O и без вердикта LLM."""
        return self.policy.should_open(candidate, verdict=None)

    def process(
        self,
        session: IncidentSession,
        candidate: Candidate,
        *,
        still_in_head: bool | None = None,
        now: datetime | None = None,
    ) -> Incident | None:
        """Создать или дополнить инцидент для нового кандидата в транзакции вызывающего кода."""
        candidate_id = candidate.id
        if candidate_id is None:
            raise ValueError("candidate.id is required before incident processing")
        if not self.decide(candidate):
            return None

        when = now if now is not None else utc_now()
        incident = session.get_incident_by_fingerprint(
            candidate.repository_id, candidate.value_fingerprint
        )
        if incident is None:
            incident = Incident(
                repository_id=candidate.repository_id,
                fingerprint=candidate.value_fingerprint,
                secret_type=candidate.secret_type,
                severity=Severity.HIGH,
                confidence=candidate.score_total,
                detector_flags=candidate.detector_flags,
                evidence_masked=candidate.value_masked,
                reason=f"Trusted local format: {candidate.secret_type.name}",
                recommendation=_RECOMMENDATION,
                first_commit_id=candidate.commit_id,
                still_in_head=still_in_head,
                created_at=when,
                updated_at=when,
            )
            incident = session.save_incident(incident)
            if incident.id is None:
                raise ValueError("incident.id is required after save")
            session.save_incident_event(
                IncidentEvent(
                    incident_id=incident.id,
                    kind=IncidentEventKind.OPENED,
                    actor="system",
                    created_at=when,
                )
            )
            previous_version = incident.version
            occurrence = incident.add_occurrence(candidate, ai_task_id=None, now=when)
            session.save_occurrence(occurrence)
            target_severity = self.policy.bump_severity(Severity.HIGH, still_in_head=still_in_head)
            if target_severity > incident.severity:
                session.save_incident_event(
                    incident.raise_severity(target_severity, now=when)
                )
            session.save_incident(incident, expected_version=previous_version)
            return incident

        incident_id = incident.id
        if incident_id is None:
            raise ValueError("incident.id is required after lookup")
        if session.has_occurrence(incident_id, candidate_id):
            return incident

        previous_version = incident.version
        if incident.status in (
            IncidentStatus.REMEDIATED,
            IncidentStatus.FALSE_POSITIVE,
            IncidentStatus.ACCEPTED_RISK,
        ):
            session.save_incident_event(
                incident.set_status(IncidentStatus.OPEN, "system", "Secret detected again", now=when)
            )
        occurrence = incident.add_occurrence(candidate, ai_task_id=None, now=when)
        session.save_occurrence(occurrence)
        session.save_incident_event(
            IncidentEvent(
                incident_id=incident.id,
                kind=IncidentEventKind.OCCURRENCE_ADDED,
                actor="system",
                created_at=when,
            )
        )
        target_severity = self.policy.bump_severity(Severity.HIGH, still_in_head=still_in_head)
        if target_severity > incident.severity:
            session.save_incident_event(incident.raise_severity(target_severity, now=when))
        incident.confidence = max(incident.confidence, candidate.score_total)
        incident.detector_flags |= candidate.detector_flags
        if still_in_head is not None:
            incident.still_in_head = still_in_head
        session.save_incident(incident, expected_version=previous_version)
        return incident
