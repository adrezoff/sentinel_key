"""Сквозные сценарии Git → задачи → инциденты без внешнего LLM."""

import asyncio
from datetime import timedelta
import secrets
import subprocess
from unittest.mock import patch

import pytest

from application import App
from feed import SqliteFeed
from models import IncidentStatus, Outcome, TaskKind
from models.common.clock import utc_now
from test_ai_validation_review import make_repo


def commit_file(repository, name, content):
    """Добавить новый файл и коммит с генерируемыми тестовыми данными."""
    (repository / name).write_text(content, encoding="utf-8")
    for args in [("add", name), ("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                  "commit", "-qm", "Add occurrence")]:
        subprocess.run(["git", "-C", str(repository), *args], check=True,
                       capture_output=True, timeout=20)


@pytest.mark.asyncio
async def test_three_commits_deduplicate_repeat_and_reopen(tmp_path):
    """Три коммита дают один инцидент, повтор не дублирует, новый коммит переоткрывает."""
    repository = tmp_path / "repo"
    make_repo(repository)
    content = (repository / "config.py").read_text(encoding="utf-8")
    commit_file(repository, "settings.py", content)
    commit_file(repository, "production.py", content)
    async with App(tmp_path / "scan.db", secrets.token_bytes(32)) as app:
        first = await app.scan(repository)
        assert first.outcome is Outcome.OK
        assert first.output.commits_done == 3
        assert await app.storage.read(lambda s: s.count("incidents")) == 1
        incident = await app.storage.read(lambda s: s.get_incident(1))
        assert len(await app.storage.read(lambda s: s.list_occurrences(incident.id))) == 3
        counts = await app.storage.read(lambda s: (s.count("candidates"), s.count("incident_events")))
        repeat = await app.scan(repository)
        assert repeat.output.commits_new == 0
        assert await app.storage.read(lambda s: (s.count("candidates"), s.count("incident_events"))) == counts

        def remediate(session):
            current = session.get_incident(incident.id)
            version = current.version
            event = current.set_status(IncidentStatus.REMEDIATED, "test", "Rotated")
            session.save_incident_event(event)
            session.save_incident(current, expected_version=version)

        await app.storage.run_in_transaction(remediate)
        commit_file(repository, "service.py", content)
        result = await app.scan(repository)
        assert result.outcome is Outcome.OK
        reopened = await app.storage.read(lambda s: s.get_incident(incident.id))
        assert reopened.status is IncidentStatus.OPEN
        assert await app.storage.read(lambda s: s.count("incidents")) == 1
        assert len(await app.storage.read(lambda s: s.list_occurrences(incident.id))) == 4


@pytest.mark.asyncio
async def test_resume_after_commit_worker_loses_lease(tmp_path):
    """Отмена посреди пачки и истечение аренды не оставляют частичных записей."""
    repository = tmp_path / "repo"
    make_repo(repository)
    content = (repository / "config.py").read_text(encoding="utf-8")
    commit_file(repository, "settings.py", content)
    async with App(tmp_path / "scan.db", secrets.token_bytes(32)) as app:
        # Регистрируем репозиторий и планируем задачу без выполнения воркера.
        from models import Repository, ScanTaskPayload
        saved = await app.storage.save_repository(Repository(location=str(repository.resolve()), name="repo"))
        root = await app.orchestrator.start_scan(saved.id, ScanTaskPayload())
        assert await app.orchestrator.plan_once()
        old_feed = SqliteFeed(app.storage, clock=lambda: utc_now() - timedelta(minutes=1))
        abandoned = await old_feed.claim([TaskKind.COMMIT_ANALYSIS], "lost-worker", lease_s=1)
        assert abandoned is not None
        original_analyze = app.commit_worker.analyzer.analyze_async
        analyzed = 0

        async def interrupted_analysis(*args, **kwargs):
            """Имитировать потерю процесса после анализа первого коммита пачки."""
            nonlocal analyzed
            analyzed += 1
            if analyzed == 2:
                raise asyncio.CancelledError()
            return await original_analyze(*args, **kwargs)

        heartbeat = asyncio.get_running_loop().create_future()
        try:
            with patch.object(app.commit_worker.analyzer, "analyze_async", interrupted_analysis):
                with pytest.raises(asyncio.CancelledError):
                    await app.commit_worker._process(abandoned, heartbeat)
        finally:
            heartbeat.cancel()
        assert analyzed == 2
        assert await app.storage.read(lambda s: s.count("candidates")) == 0
        result = await app.scan(repository)
        assert result.task_id == root.id and result.outcome is Outcome.OK
        recovered = await app.storage.get_task(abandoned.id)
        assert recovered.attempts == 2 and recovered.is_terminal
        assert await app.storage.read(lambda s: s.count("incidents")) == 1
        repeated = await app.scan(repository)
        assert repeated.output.commits_new == 0
