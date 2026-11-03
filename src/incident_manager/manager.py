"""Атомарное применение локальной политики инцидентов к сохранённым кандидатам."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from config import IncidentPolicy
from models import (
    AiVerdict,
    Candidate,
    Incident,
    IncidentEvent,
    IncidentEventKind,
    IncidentStatus,
    NotifyChannel,
    Occurrence,
    OutboxMessage,
    OutboxStatus,
    Severity,
)
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

    def save_outbox_message(self, message: OutboxMessage) -> OutboxMessage:
        """Сохранить сообщение outbox для доставки уведомления."""
        ...


_RECOMMENDATION = (
    "Rotate or revoke the credential, then remove it from Git history. "
    "Deleting it from the latest commit does not revoke it."
)

# Каналы уведомлений по умолчанию для каждого типа события
OUTBOX_CHANNELS = {
    IncidentEventKind.OPENED: NotifyChannel.WEBHOOK,
    IncidentEventKind.REOPENED: NotifyChannel.WEBHOOK,
    IncidentEventKind.SEVERITY_RAISED: NotifyChannel.WEBHOOK,
    IncidentEventKind.STATUS_CHANGED: NotifyChannel.WEBHOOK,
    IncidentEventKind.NOTIFIED: NotifyChannel.WEBHOOK,
}


def _make_outbox_payload(
    incident: Incident, event_kind: IncidentEventKind, old_severity: Severity | None = None
) -> dict:
    """Построить payload для outbox уведомления."""
    payload = {
        "incident_id": incident.id,
        "repository_id": incident.repository_id,
        "fingerprint": incident.fingerprint.hex(),
        "secret_type": incident.secret_type.name,
        "severity": incident.severity.name,
        "confidence": incident.confidence,
        "event": event_kind.name,
    }
    if old_severity is not None and old_severity != incident.severity:
        payload["old_severity"] = old_severity.name
    return payload


def _create_outbox_message(
    session: IncidentSession,
    incident: Incident,
    event_kind: IncidentEventKind,
    old_severity: Severity | None = None,
) -> None:
    """Создать outbox-сообщение для уведомления об изменении инцидента."""
    channel = OUTBOX_CHANNELS.get(event_kind, NotifyChannel.WEBHOOK)
    payload = _make_outbox_payload(incident, event_kind)
    message = OutboxMessage(
        incident_id=incident.id,
        channel=channel,
        payload=payload,
        status=OutboxStatus.PENDING,
    )
    session.save_outbox_message(message)


class IncidentManager:
    """Решает об инциденте и обновляет его граф внутри общей транзакции."""

    def __init__(self, policy: IncidentPolicy | None = None) -> None:
        """Получить политику явно или использовать документированные значения."""
        self.policy = policy if policy is not None else IncidentPolicy()

    def decide(self, candidate: Candidate, verdict: AiVerdict | None = None) -> bool:
        """Проверить правило с учётом вердикта LLM (если есть)."""
        return self.policy.should_open(candidate, verdict=verdict)

    def process(
        self,
        session: IncidentSession,
        candidate: Candidate,
        *,
        verdict: AiVerdict | None = None,
        still_in_head: bool | None = None,
        now: datetime | None = None,
    ) -> Incident | None:
        """Создать или дополнить инцидент для нового кандидата в транзакции вызывающего кода.

        Args:
            session: Транзакционная сессия хранилища.
            candidate: Кандидат для обработки.
            verdict: Вердикт LLM (если кандидат прошёл AI_VALIDATION).
            still_in_head: Остаётся ли секрет в HEAD.
            now: Время события (для тестов).

        Returns:
            Созданный или обновлённый инцидент, или None если инцидент не нужен.
        """
        candidate_id = candidate.id
        if candidate_id is None:
            raise ValueError("candidate.id is required before incident processing")
        if not self.decide(candidate, verdict=verdict):
            return None

        when = now if now is not None else utc_now()
        incident = session.get_incident_by_fingerprint(
            candidate.repository_id, candidate.value_fingerprint
        )

        # Определяем параметры инцидента: локальные или из AI-вердикта
        if verdict is not None and verdict.is_secret:
            # AI-ветка: используем данные из вердикта
            secret_type = verdict.secret_type
            severity = verdict.severity
            confidence = verdict.confidence
            detector_flags = candidate.detector_flags | 8  # DetectorFlags.LLM = 8
            evidence_masked = candidate.value_masked
            reason = verdict.reason
            recommendation = verdict.recommendation
        else:
            # Локальная ветка: trusted format
            secret_type = candidate.secret_type
            severity = Severity.HIGH
            confidence = candidate.score_total
            detector_flags = candidate.detector_flags
            evidence_masked = candidate.value_masked
            reason = f"Trusted local format: {candidate.secret_type.name}"
            recommendation = _RECOMMENDATION

        if incident is None:
            incident = Incident(
                repository_id=candidate.repository_id,
                fingerprint=candidate.value_fingerprint,
                secret_type=secret_type,
                severity=severity,
                confidence=confidence,
                detector_flags=detector_flags,
                evidence_masked=evidence_masked,
                reason=reason,
                recommendation=recommendation,
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
            _create_outbox_message(session, incident, IncidentEventKind.OPENED)
            previous_version = incident.version
            occurrence = incident.add_occurrence(candidate, ai_task_id=None, now=when)
            session.save_occurrence(occurrence)
            target_severity = self.policy.bump_severity(severity, still_in_head=still_in_head)
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
            _create_outbox_message(session, incident, IncidentEventKind.REOPENED)
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
        _create_outbox_message(session, incident, IncidentEventKind.OCCURRENCE_ADDED)
        # При обновлении: используем AI-вердикт если есть, иначе локальную политику
        old_severity = incident.severity
        if verdict is not None and verdict.is_secret:
            target_severity = self.policy.bump_severity(verdict.severity, still_in_head=still_in_head)
            if target_severity > incident.severity:
                session.save_incident_event(incident.raise_severity(target_severity, now=when))
            incident.confidence = max(incident.confidence, verdict.confidence)
            incident.detector_flags |= 8  # LLM flag
            incident.severity = max(incident.severity, verdict.severity)
            incident.reason = verdict.reason
            incident.recommendation = verdict.recommendation
        else:
            target_severity = self.policy.bump_severity(Severity.HIGH, still_in_head=still_in_head)
            if target_severity > incident.severity:
                session.save_incident_event(incident.raise_severity(target_severity, now=when))
            incident.confidence = max(incident.confidence, candidate.score_total)
            incident.detector_flags |= candidate.detector_flags
        if still_in_head is not None:
            incident.still_in_head = still_in_head
        if incident.severity != old_severity:
            _create_outbox_message(session, incident, IncidentEventKind.SEVERITY_RAISED, old_severity)
        session.save_incident(incident, expected_version=previous_version)
        return incident
