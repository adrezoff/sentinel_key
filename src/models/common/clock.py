"""UTC-часы для методов моделей, которым нужна метка времени."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Текущий момент в UTC (tz-aware)."""
    return datetime.now(timezone.utc)
