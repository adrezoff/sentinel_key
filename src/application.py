"""Локальная точка сборки MVP и CLI для сканирования Git-репозитория."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import os
from pathlib import Path

from feed import SqliteFeed
from git_collector import LocalGitCollector
from incident_manager import IncidentManager
from models import (
    Outcome,
    Repository,
    ScanMode,
    ScanTaskPayload,
    TaskKind,
    TaskResult,
)
from orchestrator import ScanOrchestrator
from preprocessor import Preprocessor
from static_analyzer import StaticAnalyzer
from storage import SqliteStorage
from workers import CommitWorker, WorkerSettings


class App:
    """Владеет SQLite и выполняет локальный scan до терминального результата."""

    def __init__(
        self,
        database: str | Path,
        fingerprint_key: bytes,
        *,
        poll_interval_s: float = 0.25,
    ) -> None:
        if not isinstance(fingerprint_key, bytes) or len(fingerprint_key) < 32:
            raise ValueError("fingerprint_key must contain at least 32 bytes")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self.storage = SqliteStorage(database)
        self.feed = SqliteFeed(self.storage)
        self.orchestrator = ScanOrchestrator(
            feed=self.feed,
            storage=self.storage,
            collector_for=LocalGitCollector,
        )
        self.commit_worker = CommitWorker(
            feed=self.feed,
            storage=self.storage,
            collector_for=LocalGitCollector,
            preprocessor=Preprocessor(fingerprint_key),
            analyzer=StaticAnalyzer(),
            incident_manager=IncidentManager(),
            settings=WorkerSettings("local-commit-worker", (TaskKind.COMMIT_ANALYSIS,)),
        )
        self.poll_interval_s = poll_interval_s
        self._opened = False

    async def __aenter__(self) -> App:
        """Открыть и мигрировать SQLite перед сканированием."""
        await self.storage.initialize()
        self._opened = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Закрыть SQLite при выходе из контекста."""
        self._opened = False
        await self.storage.close()

    async def scan(
        self, repository_path: str | Path, payload: ScanTaskPayload | None = None
    ) -> TaskResult:
        """Создать или возобновить запуск для локального Git и дождаться итога."""
        if not self._opened:
            raise RuntimeError("App must be opened with async with")
        path = Path(repository_path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("repository path must be an existing directory")
        selected_payload = payload if payload is not None else ScanTaskPayload(ai_enabled=False)
        if selected_payload.ai_enabled:
            raise ValueError("AI validation is not available in the local MVP")
        repository = await self.storage.run_in_transaction(
            lambda session: session.get_repository_by_location(str(path))
            or session.save_repository(Repository(location=str(path), name=path.name))
        )
        root = await self.orchestrator.start_scan(repository.id, selected_payload)
        while True:
            progress = bool(await self.feed.reap_expired())
            progress = await self.orchestrator.plan_once() or progress
            progress = await self.commit_worker.run_once() or progress
            progress = bool(await self.orchestrator.finalize_ready()) or progress
            current = await self.storage.get_task(root.id)
            if current is None:
                raise RuntimeError("scan task disappeared")
            if current.is_terminal:
                result = await self.storage.get_task_result(root.id)
                if result is None:
                    raise RuntimeError("scan finished without a result")
                return result
            if not progress:
                await asyncio.sleep(self.poll_interval_s)


def _parser() -> argparse.ArgumentParser:
    """Описать минимальный интерфейс запуска для локального Git."""
    parser = argparse.ArgumentParser(description="Поиск секретов в локальном Git-репозитории")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="Выполнить или возобновить анализ")
    scan.add_argument("--repository", required=True, help="Путь к локальному Git-репозиторию")
    scan.add_argument("--database", default="var/sentinel_key.db", help="Путь к SQLite")
    scan.add_argument(
        "--mode", choices=("incremental", "full", "targeted"), default="incremental"
    )
    scan.add_argument("--ref", action="append", dest="refs", help="Git ref; можно повторять")
    scan.add_argument(
        "--target-sha", action="append", dest="target_shas", help="SHA для режима targeted"
    )
    scan.add_argument("--batch-size", type=int, default=50)
    return parser


def _load_fingerprint_key() -> bytes:
    """Прочитать стабильный HMAC-ключ из environment без вывода значения."""
    encoded = os.environ.get("SENTINEL_KEY_HMAC_KEY_B64")
    if not encoded:
        raise ValueError("SENTINEL_KEY_HMAC_KEY_B64 is required")
    try:
        key = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError("SENTINEL_KEY_HMAC_KEY_B64 must be valid base64") from exc
    if len(key) < 32:
        raise ValueError("SENTINEL_KEY_HMAC_KEY_B64 must decode to at least 32 bytes")
    return key


async def _run_scan(args: argparse.Namespace) -> TaskResult:
    """Собрать payload и выполнить одну CLI-команду."""
    payload = ScanTaskPayload(
        mode={
            "incremental": ScanMode.INCREMENTAL,
            "full": ScanMode.FULL,
            "targeted": ScanMode.TARGETED,
        }[args.mode],
        batch_size=args.batch_size,
        refs=args.refs if args.refs is not None else ["--all"],
        target_shas=args.target_shas or [],
        ai_enabled=False,
    )
    async with App(args.database, _load_fingerprint_key()) as app:
        return await app.scan(args.repository, payload)


def main() -> int:
    """Запустить CLI и вернуть код завершения по Outcome."""
    parser = _parser()
    args = parser.parse_args()
    try:
        result = asyncio.run(_run_scan(args))
    except ValueError as exc:
        parser.exit(2, f"{exc}\n")
    except Exception as exc:
        parser.exit(1, f"scan failed: {type(exc).__name__}\n")
    summary = result.output
    if summary is None:
        print(
            f"run_id={result.task_id} outcome={result.outcome.name} "
            f"error_code={result.error_code.name if result.error_code else 'NONE'}"
        )
    else:
        print(
            f"run_id={result.task_id} outcome={result.outcome.name} "
            f"commits_total={summary.commits_total} commits_new={summary.commits_new} "
            f"commits_done={summary.commits_done} commits_failed={summary.commits_failed} "
            f"candidates={summary.candidates} incidents_opened={summary.incidents_opened}"
        )
    return 0 if result.outcome is Outcome.OK else 2 if result.outcome is Outcome.PARTIAL else 1


if __name__ == "__main__":
    raise SystemExit(main())
