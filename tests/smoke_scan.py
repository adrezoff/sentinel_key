"""Локальная сквозная проверка orchestrator и commit worker без внешних сервисов."""

from __future__ import annotations

import asyncio
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from feed import SqliteFeed
from git_collector import LocalGitCollector
from incident_manager import IncidentManager
from models import Outcome, Repository, ScanTaskPayload, TaskKind
from orchestrator import ScanOrchestrator
from preprocessor import Preprocessor
from static_analyzer import StaticAnalyzer
from storage import SqliteStorage
from workers import CommitWorker, WorkerSettings


def _git(repository: Path, *args: str) -> None:
    """Выполнить локальный Git для временного fixture-репозитория."""
    subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
    )


async def _scan(repository_path: Path, database_path: Path) -> tuple[int, int]:
    """Проверить первый и повторный scan без вывода значений кандидатов."""
    async with SqliteStorage(database_path) as storage:
        repository = await storage.save_repository(
            Repository(location=str(repository_path), name="smoke")
        )
        feed = SqliteFeed(storage)
        orchestrator = ScanOrchestrator(
            feed=feed, storage=storage, collector_for=LocalGitCollector
        )
        worker = CommitWorker(
            feed=feed,
            storage=storage,
            collector_for=LocalGitCollector,
            preprocessor=Preprocessor(secrets.token_bytes(32)),
            analyzer=StaticAnalyzer(),
            incident_manager=IncidentManager(),
            settings=WorkerSettings("smoke-commit", (TaskKind.COMMIT_ANALYSIS,)),
        )
        first = await orchestrator.start_scan(
            repository.id, ScanTaskPayload(ai_enabled=False)
        )
        assert await orchestrator.plan_once()
        while await worker.run_once():
            pass
        assert await orchestrator.finalize_ready() == [first.id]
        first_result = await storage.get_task_result(first.id)
        assert first_result.outcome is Outcome.OK
        assert first_result.output.commits_total == 1
        assert first_result.output.commits_done == 1
        assert first_result.output.candidates > 0
        candidates = await storage.read(lambda session: session.count("candidates"))

        second = await orchestrator.start_scan(
            repository.id, ScanTaskPayload(ai_enabled=False)
        )
        assert await orchestrator.plan_once()
        second_result = await storage.get_task_result(second.id)
        assert second_result.outcome is Outcome.OK
        assert second_result.output.commits_new == 0
        assert await storage.read(lambda session: session.count("candidates")) == candidates
        return first_result.output.commits_done, candidates


def main() -> None:
    """Создать временный Git-репозиторий и напечатать безопасные счётчики."""
    with tempfile.TemporaryDirectory(prefix="sentinel-smoke-") as directory:
        root = Path(directory)
        repository = root / "repository"
        repository.mkdir()
        _git(repository, "init", "-q")
        generated_value = secrets.token_urlsafe(32)
        (repository / ".env").write_text(
            f"DATABASE_PASSWORD={generated_value}\n", encoding="utf-8"
        )
        _git(repository, "add", ".env")
        _git(
            repository,
            "-c", "user.name=Smoke",
            "-c", "user.email=smoke@example.invalid",
            "commit", "-qm", "Add generated smoke fixture",
        )
        commits_done, candidates = asyncio.run(_scan(repository, root / "scan.db"))
    print(f"SMOKE OK: commits_done={commits_done}, candidates={candidates}, repeat_new=0")


if __name__ == "__main__":
    main()
