"""``Occurrence`` — incident × candidate (table ``occurrences``)."""

from dataclasses import dataclass, field
from datetime import datetime

from ..common.clock import utc_now


@dataclass(slots=True)
class Occurrence:
    incident_id: int
    candidate_id: int
    seen_at: datetime = field(default_factory=utc_now)
    ai_task_id: int | None = None
