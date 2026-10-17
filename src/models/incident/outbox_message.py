"""``OutboxMessage`` — notification outbox (table ``outbox_messages``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any

from ..common.clock import utc_now


class NotifyChannel(IntEnum):
    WEBHOOK = 1
    SLACK = 2
    EMAIL = 3


class OutboxStatus(IntEnum):
    PENDING = 1
    SENT = 2
    FAILED = 3


@dataclass(slots=True)
class OutboxMessage:
    incident_id: int
    channel: NotifyChannel
    payload: dict[str, Any]
    id: int | None = None
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    not_before: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    sent_at: datetime | None = None
