"""Асинхронная оболочка над стандартным ``sqlite3``."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from models import Candidate, Commit, Repository, Task, TaskResult

from .exceptions import StorageError, StorageNotInitializedError
from .session import StorageSession

T = TypeVar("T")
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class SqliteStorage:
    """SQLite-хранилище с неблокирующим для event loop публичным API."""

    def __init__(self, database: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.database = str(database)
        self.busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "SqliteStorage":
        """Открыть и инициализировать хранилище для async context manager."""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Закрыть хранилище при выходе из async context manager."""
        await self.close()

    async def initialize(self) -> None:
        """Открыть соединение, настроить SQLite и применить миграции."""
        async with self._lock:
            if self._connection is not None:
                return
            try:
                self._connection = await asyncio.to_thread(self._open_and_migrate)
            except Exception as exc:
                self._connection = None
                if isinstance(exc, (StorageError, ValueError)):
                    raise
                raise StorageError(f"failed to initialize SQLite database: {exc}") from exc

    async def close(self) -> None:
        """Закрыть соединение; повторный вызов безопасен."""
        async with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                await asyncio.to_thread(connection.close)

    async def run_in_transaction(self, operation: Callable[[StorageSession], T]) -> T:
        """Выполнить callback атомарно внутри ``BEGIN IMMEDIATE``."""
        async with self._lock:
            connection = self._require_connection()
            return await asyncio.to_thread(self._run_transaction, connection, operation)

    async def read(self, operation: Callable[[StorageSession], T]) -> T:
        """Выполнить короткую операцию чтения без открывающей запись блокировки."""
        async with self._lock:
            connection = self._require_connection()
            return await asyncio.to_thread(operation, StorageSession(connection))

    async def save_repository(self, repository: Repository) -> Repository:
        """Сохранить репозиторий в отдельной транзакции."""
        return await self.run_in_transaction(lambda session: session.save_repository(repository))

    async def get_repository(self, repository_id: int) -> Repository | None:
        """Загрузить репозиторий по id."""
        return await self.read(lambda session: session.get_repository(repository_id))

    async def save_commit(self, commit: Commit) -> Commit:
        """Идемпотентно сохранить коммит в отдельной транзакции."""
        return await self.run_in_transaction(lambda session: session.save_commit(commit))

    async def get_commit(self, commit_id: int) -> Commit | None:
        """Загрузить коммит по id."""
        return await self.read(lambda session: session.get_commit(commit_id))

    async def save_task(self, task: Task) -> Task:
        """Идемпотентно сохранить задачу в отдельной транзакции."""
        return await self.run_in_transaction(lambda session: session.save_task(task))

    async def get_task(self, task_id: int) -> Task | None:
        """Загрузить задачу по id."""
        return await self.read(lambda session: session.get_task(task_id))

    async def save_task_result(self, result: TaskResult) -> None:
        """Сохранить результат задачи в отдельной транзакции."""
        await self.run_in_transaction(lambda session: session.save_task_result(result))

    async def get_task_result(self, task_id: int) -> TaskResult | None:
        """Загрузить результат задачи по id."""
        return await self.read(lambda session: session.get_task_result(task_id))

    async def save_candidate(self, candidate: Candidate) -> Candidate:
        """Идемпотентно сохранить кандидата в отдельной транзакции."""
        return await self.run_in_transaction(lambda session: session.save_candidate(candidate))

    async def get_candidate(self, candidate_id: int) -> Candidate | None:
        """Загрузить кандидата по id."""
        return await self.read(lambda session: session.get_candidate(candidate_id))

    async def schema_version(self) -> int:
        """Вернуть последнюю применённую версию миграции."""
        def load(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])

        async with self._lock:
            return await asyncio.to_thread(load, self._require_connection())

    async def journal_mode(self) -> str:
        """Вернуть текущий режим журнала SQLite для диагностики."""
        def load(connection: sqlite3.Connection) -> str:
            row = connection.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).lower()

        async with self._lock:
            return await asyncio.to_thread(load, self._require_connection())

    def _require_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            raise StorageNotInitializedError("storage is not initialized")
        return connection

    def _open_and_migrate(self) -> sqlite3.Connection:
        if self.database != ":memory:":
            Path(self.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            if self.database != ":memory:":
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
            self._apply_migrations(connection)
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _apply_migrations(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                applied_at INTEGER NOT NULL
            )
            """
        )
        applied = {
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
        migrations_dir = Path(__file__).with_name("migrations")
        migrations: list[tuple[int, Path]] = []
        for path in migrations_dir.glob("*.sql"):
            match = _MIGRATION_NAME.fullmatch(path.name)
            if match is None:
                raise StorageError(f"invalid migration filename: {path.name}")
            migrations.append((int(match.group(1)), path))
        versions = [version for version, _ in migrations]
        if len(versions) != len(set(versions)):
            raise StorageError("duplicate migration version")
        for version, path in sorted(migrations):
            if version in applied:
                continue
            script = path.read_text(encoding="utf-8")
            applied_at = int(time.time() * 1000)
            name = path.name.replace("'", "''")
            try:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    f"{script}\n"
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    f"VALUES ({version}, '{name}', {applied_at});\n"
                    "COMMIT;"
                )
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

    @staticmethod
    def _run_transaction(
        connection: sqlite3.Connection, operation: Callable[[StorageSession], T]
    ) -> T:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = operation(StorageSession(connection))
        except BaseException:
            connection.rollback()
            raise
        connection.commit()
        return result
