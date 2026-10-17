"""Общие хелперы нескольких пакетов моделей (не доменные сущности)."""

from .clock import utc_now
from .codec import from_jsonable, to_jsonable

__all__ = ["from_jsonable", "to_jsonable", "utc_now"]
