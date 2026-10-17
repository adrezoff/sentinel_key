"""``Repository`` — отслеживаемый Git-репозиторий (таблица ``repositories``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..common.clock import utc_now


@dataclass(slots=True)
class Repository:
    """Репозиторий, по которому система строит запуски сканирования.

    ``location`` — URL или локальный путь (UNIQUE). ``version`` — optimistic lock.
    """

    location: str
    name: str
    id: int | None = None
    default_ref: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    last_scan_at: datetime | None = None
    last_scan_task_id: int | None = None
    version: int = 1
