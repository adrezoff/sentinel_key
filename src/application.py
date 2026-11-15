"""Точка сборки и CLI для сканирования локальных и удалённых Git-репозиториев."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import os
from pathlib import Path
from functools import partial
from urllib.parse import urlsplit

from feed import SqliteFeed
from git_collector import (
    GitCollectorConfig, SourceKind, create_git_collector, infer_source_kind,
    normalize_remote_url,
)
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
from workers.ai import AiWorker, create_ai_worker
from workers.ai_factory import DefaultAiTaskFactory
from ai_validation.llm_client import LlmConfig


class App:
    """Владеет SQLite и выполняет scan пути или URL до терминального результата."""

    def __init__(
        self,
        database: str | Path,
        fingerprint_key: bytes,
        *,
        poll_interval_s: float = 0.25,
        ai_enabled: bool = False,
        llm_config: LlmConfig | None = None,
        use_fake_llm: bool = False,
        git_config: GitCollectorConfig | None = None,
        git_cache: str | Path | None = None,
    ) -> None:
        if not isinstance(fingerprint_key, bytes) or len(fingerprint_key) < 32:
            raise ValueError("fingerprint_key must contain at least 32 bytes")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        resolved_llm_config = llm_config
        if ai_enabled and resolved_llm_config is None:
            resolved_llm_config = LlmConfig.from_env()
        self.storage = SqliteStorage(database)
        self.feed = SqliteFeed(self.storage)
        self.git_config = git_config or GitCollectorConfig()
        collector_for = partial(
            create_git_collector, config=self.git_config, mirror_root=git_cache,
        )
        self.orchestrator = ScanOrchestrator(
            feed=self.feed,
            storage=self.storage,
            collector_for=collector_for,
        )
        self.commit_worker = CommitWorker(
            feed=self.feed,
            storage=self.storage,
            collector_for=collector_for,
            preprocessor=Preprocessor(fingerprint_key),
            analyzer=StaticAnalyzer(),
            incident_manager=IncidentManager(),
            settings=WorkerSettings("local-commit-worker", (TaskKind.COMMIT_ANALYSIS,)),
            ai_task_factory=(
                DefaultAiTaskFactory(resolved_llm_config) if ai_enabled else None
            ),
        )
        self.ai_worker = None
        if ai_enabled:
            self.ai_worker = create_ai_worker(
                feed=self.feed,
                storage=self.storage,
                incident_manager=IncidentManager(),
                llm_config=resolved_llm_config or LlmConfig(),
                use_fake_llm=use_fake_llm,
            )
        self.poll_interval_s = poll_interval_s
        self._opened = False
        self._ai_enabled = ai_enabled

    async def __aenter__(self) -> App:
        """Открыть и мигрировать SQLite перед сканированием."""
        await self.storage.initialize()
        self._opened = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Освободить LLM-транспорт и SQLite при любом выходе из контекста."""
        self._opened = False
        try:
            if self.ai_worker is not None:
                await self.ai_worker.aclose()
        finally:
            await self.storage.close()

    async def scan(
        self, repository_path: str | Path, payload: ScanTaskPayload | None = None
    ) -> TaskResult:
        """Зарегистрировать путь или clone URL, создать запуск и дождаться итога."""
        if not self._opened:
            raise RuntimeError("App must be opened with async with")
        source = str(repository_path)
        if infer_source_kind(source) is SourceKind.LOCAL:
            path = Path(source).expanduser().resolve()
            if not await asyncio.to_thread(path.is_dir):
                raise ValueError("repository path must be an existing directory")
            location, name = str(path), path.name
        else:
            location = normalize_remote_url(source, allow_http=self.git_config.allow_http)
            name = urlsplit(location).path.rsplit("/", 1)[-1]
        selected_payload = payload if payload is not None else ScanTaskPayload(ai_enabled=self._ai_enabled)
        repository = await self.storage.run_in_transaction(
            lambda session: session.get_repository_by_location(location)
            or session.save_repository(Repository(location=location, name=name))
        )
        root = await self.orchestrator.start_scan(repository.id, selected_payload)

        # Активный запуск с AI-задачами требует включённого AI-воркера.
        if not self._ai_enabled:
            pending_ai_tasks = await self.storage.read(
                lambda session: any(
                    t.kind is TaskKind.AI_VALIDATION and not t.is_terminal
                    for t in session.list_run_tasks(root.id)
                )
            )
            if pending_ai_tasks:
                raise RuntimeError(
                    "Cannot resume scan: run has pending AI_VALIDATION tasks but AI is disabled. "
                    "Re-run with ai_enabled=True to process AI tasks."
                )

        while True:
            progress = bool(await self.feed.reap_expired())
            progress = await self.orchestrator.plan_once() or progress
            progress = await self.commit_worker.run_once() or progress
            if self.ai_worker is not None:
                progress = await self.ai_worker.run_once() or progress
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
    """Описать интерфейс запуска для локального пути и удалённого clone URL."""
    parser = argparse.ArgumentParser(description="Поиск секретов в Git-репозитории")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="Выполнить или возобновить анализ")
    scan.add_argument("--repository", required=True, help="Локальный путь или HTTPS clone URL")
    scan.add_argument("--git-cache", default="var/git-mirrors", help="Общий каталог зеркал и лимитов запросов")
    scan.add_argument("--allow-http", action="store_true", help="Разрешить незашифрованный HTTP clone URL")
    scan.add_argument("--database", default="var/sentinel_key.db", help="Путь к SQLite")
    scan.add_argument(
        "--mode", choices=("incremental", "full", "targeted"), default="incremental"
    )
    scan.add_argument("--ref", action="append", dest="refs", help="Git ref; можно повторять")
    scan.add_argument(
        "--target-sha", action="append", dest="target_shas", help="SHA для режима targeted"
    )
    scan.add_argument("--batch-size", type=int, default=50)
    scan.add_argument("--ai-enabled", action="store_true", help="Включить AI-валидацию (требует SENTINEL_KEY_LLM_API_KEY)")
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
        ai_enabled=args.ai_enabled,
    )
    use_fake_llm = os.getenv("SENTINEL_KEY_USE_FAKE_LLM", "").lower() in ("1", "true", "yes")
    async with App(
        args.database,
        _load_fingerprint_key(),
        ai_enabled=args.ai_enabled,
        use_fake_llm=use_fake_llm,
        git_config=GitCollectorConfig(allow_http=args.allow_http),
        git_cache=args.git_cache,
    ) as app:
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
