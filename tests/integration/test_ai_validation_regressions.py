"""Проверки поведения исправлений ревью; внешняя сеть не используется."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import secrets
import pytest
from review_ai_branch_fixtures import candidate, verdict
from test_ai_validation_review import make_repo, seed, worker
from models import (
    AiValidationPayload, Commit, IncidentStatus, Outcome, Repository,
    Task, TaskKind, TaskStatus, TaskError, ErrorCode,
)
from storage import SqliteStorage
from incident_manager import IncidentManager
from application import App
from ai_validation.masking_guard import check_masking

@pytest.mark.asyncio
async def test_negative_does_not_close_other_confirmed_occurrence():
    """Отрицательный вердикт другого кандидата сохраняет действующее подтверждение."""
    async with SqliteStorage(':memory:') as s:
        r=await s.save_repository(Repository(location='repo',name='repo'))
        co=await s.save_commit(Commit(repository_id=r.id,sha=b'a'*20))
        c1=await s.save_candidate(candidate(r.id,co.id))
        c2=await s.save_candidate(replace(candidate(r.id,co.id),line_number=2))
        manager=IncidentManager()
        t1=await s.save_task(Task.ai_validation(repository_id=r.id,payload=AiValidationPayload(c1.id,2)))
        v1=await s.save_ai_verdict(verdict(c1,t1))
        inc=await s.run_in_transaction(lambda session:manager.process(session,c1,verdict=v1))
        t2=await s.save_task(Task.ai_validation(repository_id=r.id,payload=AiValidationPayload(c2.id,2)))
        v2=await s.save_ai_verdict(verdict(c2,t2,is_secret=False))
        await s.run_in_transaction(lambda session:manager.process(session,c2,verdict=v2))
        stored=await s.read(lambda session:session.get_incident(inc.id))
        assert stored.status is IncidentStatus.OPEN, f'Other confirmed occurrence lost: {stored.status.name}'

@pytest.mark.asyncio
async def test_suspicious_below_threshold_not_sent_to_ai(tmp_path):
    """Новый кандидат ниже порога AI остаётся на локальном маршруте."""
    repo=tmp_path/'repo'
    make_repo(repo)
    import subprocess
    (repo/'config.py').write_text('password="'+''.join(secrets.choice('ABCDEFGHabcdefgh23456789') for _ in range(8))+'"\n',encoding='utf-8')
    subprocess.run(['git','-C',str(repo),'add','.'],check=True,capture_output=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=Review','-c','user.email=review@example.invalid','commit','-qm','Password'],check=True,capture_output=True)
    async with App(tmp_path/'db',secrets.token_bytes(32),ai_enabled=True,use_fake_llm=True) as app:
        captured=[]
        async def complete(c,p):
            captured.append(c.score_total)
            return verdict(c,SimpleNamespace(id=0),is_secret=False)
        app.ai_worker.llm_client=SimpleNamespace(complete=complete)
        await app.scan(repo)
        assert not any(score < .7 for score in captured), f'Below-threshold candidates sent to AI: {captured}'

def test_masked_database_url_allowed():
    """Маска пароля URL совместима с guard."""
    assert check_masking('postgresql://user:abcd...wxyz@localhost/db').ok

@pytest.mark.asyncio
async def test_trusted_format_fallback_when_llm_fails(tmp_path):
    """Исчерпание попыток LLM создаёт локальный инцидент и сохраняет PARTIAL."""
    from ai_validation.llm_client import LlmNetworkError
    repo=tmp_path/'repo'
    make_repo(repo)
    async with App(tmp_path/'db',secrets.token_bytes(32),ai_enabled=True,use_fake_llm=True) as app:
        original=app.commit_worker.ai_task_factory
        def factory(*args,**kwargs):
            task=original(*args,**kwargs)
            task.max_attempts=1
            return task
        app.commit_worker.ai_task_factory=factory
        app.ai_worker.llm_client=SimpleNamespace(complete=AsyncMock(side_effect=LlmNetworkError('offline')))
        result=await app.scan(repo)
        assert result.outcome is Outcome.PARTIAL
        count=await app.storage.read(lambda s:s.count('incidents'))
        assert count==1, 'Trusted-format fallback lost when AI fails'


@pytest.mark.parametrize('scheme', ['postgresql', 'mysql', 'mongodb', 'redis'])
@pytest.mark.parametrize('password', ['abcd...wxyz', '********'])
def test_url_mask_and_raw_password_are_distinguished(scheme, password):
    """Исключение для масок не разрешает сырой пароль из-за маски в имени хоста."""
    assert check_masking(f'{scheme}://user:{password}@localhost/db').ok
    assert not check_masking(f'{scheme}://user:rawpassword123@abcd...wxyz/db').ok


@pytest.mark.asyncio
async def test_retry_does_not_create_fallback_incident():
    """До исчерпания попыток предварительный локальный инцидент не создаётся."""
    from ai_validation.llm_client import LlmNetworkError
    async with SqliteStorage(':memory:') as s:
        c, feed, task = await seed(s)
        client = SimpleNamespace(complete=AsyncMock(side_effect=LlmNetworkError('offline')))
        await worker(s, feed, client).run_once()
        assert (await s.get_task(task.id)).status is TaskStatus.QUEUED
        assert await s.get_task_result(task.id) is None
        assert await s.read(lambda session: session.count('incidents')) == 0


@pytest.mark.asyncio
async def test_terminal_failure_rolls_back_incident_and_result_together():
    """Ошибка после создания fallback откатывает граф и терминальный статус задачи."""
    async with SqliteStorage(':memory:') as s:
        c, feed, task = await seed(s)
        c.score_total = .95
        claimed = await feed.claim([TaskKind.AI_VALIDATION], 'review', 30)
        manager = IncidentManager()
        def failing_effects(session):
            """Записать граф и имитировать сбой до сохранения TaskResult."""
            assert manager.process(session, c) is not None
            raise RuntimeError('rollback probe')
        with pytest.raises(RuntimeError, match='rollback probe'):
            await feed.fail(claimed, TaskError(ErrorCode.LLM_FAILED, 'offline', False),
                            side_effects=failing_effects)
        assert (await s.get_task(task.id)).status is TaskStatus.RUNNING
        assert await s.get_task_result(task.id) is None
        counts = await s.read(lambda session: tuple(session.count(name) for name in
            ('incidents', 'occurrences', 'incident_events', 'outbox_messages')))
        assert counts == (0, 0, 0, 0)

