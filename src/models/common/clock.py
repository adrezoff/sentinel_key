"""UTC clock used by model methods that need a timestamp."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
