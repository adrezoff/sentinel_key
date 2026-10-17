"""``IncidentEvent`` — append-only журнал инцидента (таблица ``incident_events``)."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum

from ..common.clock import utc_now


class IncidentEventKind(IntEnum):
    """Вид события жизненного цикла инцидента."""

    OPENED = 1
    OCCURRENCE_ADDED = 2
    SEVERITY_RAISED = 3
    STATUS_CHANGED = 4
    REOPENED = 5
    NOTIFIED = 6


@dataclass(slots=True)
class IncidentEvent:
    """Запись журнала: кто/когда/что изменил. Не редактируется после создания."""

    incident_id: int
    kind: IncidentEventKind
    id: int | None = None
    note: str | None = None
    actor: str | None = None
    created_at: datetime = field(default_factory=utc_now)
