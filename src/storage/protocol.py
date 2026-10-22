"""Интерфейс хранилища для модулей приложения."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class Storage(Protocol):
    """Асинхронная граница хранилища с поддержкой составных транзакций."""

    async def initialize(self) -> None:
        """Открыть хранилище и применить миграции."""
        ...

    async def close(self) -> None:
        """Закрыть соединение и освободить ресурсы."""
        ...

    async def run_in_transaction(self, operation: Callable[[Any], T]) -> T:
        """Выполнить синхронный callback атомарно с немедленной блокировкой записи."""
        ...

    async def read(self, operation: Callable[[Any], T]) -> T:
        """Выполнить короткую операцию чтения без блокировки записи."""
        ...
