import asyncio
import secrets
import string
import subprocess
from datetime import timedelta
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from review_ai_branch_fixtures import candidate, verdict, response, client_with_response
from ai_validation.llm_client import build_prompt
from application import App
from feed import SqliteFeed, LeaseLostError
from incident_manager import IncidentManager
from models import (
    AiValidationOutput, AiValidationPayload, AiVerdict, Commit, ErrorCode, IncidentStatus,
    Outcome, Repository, ScanMode, ScanTaskPayload, SecretType, Severity, Task, TaskKind,
    TaskResult, TaskStatus,
)
from models.common.clock import utc_now
from storage import SqliteStorage, TaskLeaseLostError
from workers.ai import AiWorker
from workers.base import WorkerSettings


def make_repo(path, *, password_neighbor=False):
    """Создать настоящий Git с синтетическим токеном и достаточной энтропией."""
    path.mkdir()
    alphabet = string.ascii_letters + string.digits
    generated = 'ghp_' + ''.join(secrets.SystemRandom().sample(alphabet, len(alphabet)))
    text = 'api_key="' + generated + '"\n'
    if password_neighbor:
        text += 'password="' + secrets.token_urlsafe(32) + '"\n'
    (path/'config.py').write_text(text, encoding='utf-8')
    def git(*args):
        return subprocess.run(['git','-C',str(path),*args],check=True,capture_output=True,text=True).stdout.strip()
    git('init','-q')
    git('add','config.py')
    git('-c','user.name=Review','-c','user.email=review@example.invalid','commit','-qm','Generated fixture')
    return git('rev-parse','HEAD')


def fake_verdict(*, is_secret=False):
    """Вернуть ответ с корректной схемой для управляемого результата LLM."""
    return AiVerdict(task_id=0,candidate_id=0,is_secret=is_secret,
        secret_type=SecretType.GITHUB_TOKEN,confidence=.99,severity=Severity.HIGH,
        reason='review verdict',recommendation='review recommendation',model='fake',prompt_version=1)


async def seed(s):
    """Сохранить минимальный корректный граф кандидата и задачи."""
    r=await s.save_repository(Repository(location='review',name='review'))
    co=await s.save_commit(Commit(repository_id=r.id,sha=b'a'*20))
    c=await s.save_candidate(candidate(r.id,co.id))
    f=SqliteFeed(s)
    t=await f.enqueue(Task.ai_validation(repository_id=r.id,payload=AiValidationPayload(c.id,1)))
    return c,f,t


def worker(s,f,client,lease_s=30):
    """Использовать настоящий worker с настоящими storage, feed и manager."""
    return AiWorker(feed=f,storage=s,incident_manager=IncidentManager(),llm_client=client,
        settings=WorkerSettings('review',(TaskKind.AI_VALIDATION,),lease_s=lease_s))


@pytest.mark.asyncio
async def test_masked_neighbor_must_not_fail_ai_task(tmp_path):
    """Маскированный соседний пароль не должен запрещать анализ кандидата."""
    repo=tmp_path/'repo'
    make_repo(repo,password_neighbor=True)
    async with App(tmp_path/'mask.db',secrets.token_bytes(32),ai_enabled=True,use_fake_llm=True) as app:
        app.ai_worker.llm_client=SimpleNamespace(complete=AsyncMock(return_value=fake_verdict()))
        result=await app.scan(repo)
        tasks=await app.storage.read(lambda session:session.list_run_tasks(result.task_id))
        failures=[(t.status.name,t.error_code.name if t.error_code else None)
            for t in tasks if t.kind is TaskKind.AI_VALIDATION and t.status is TaskStatus.FAILED]
        assert failures == [], f'AI failures for correctly masked pipeline output: {failures}'


@pytest.mark.asyncio
async def test_real_pipeline_prompt_must_include_source_line(tmp_path):
    """Проверить prompt на реальном результате preprocessor."""
    repo=tmp_path/'repo'
    make_repo(repo)
    async with App(tmp_path/'prompt.db',secrets.token_bytes(32),ai_enabled=True,use_fake_llm=True) as app:
        seen=[]
        async def complete(c,p):
            seen.append(c)
            return fake_verdict()
        app.ai_worker.llm_client=SimpleNamespace(complete=complete)
        await app.scan(repo)
        assert len(seen)==1
        c=seen[0]
        assert c.context_masked == ''
        assert c.source_line_masked in build_prompt(c)[1], 'Actual source line is missing from the prompt'


@pytest.mark.asyncio
async def test_negative_verdict_must_veto_local_incident(tmp_path):
    """Отрицательный финальный вердикт должен отменять решение для этой находки."""
    repo=tmp_path/'repo'
    make_repo(repo)
    async with App(tmp_path/'veto.db',secrets.token_bytes(32),ai_enabled=True,use_fake_llm=True) as app:
        app.ai_worker.llm_client=SimpleNamespace(complete=AsyncMock(return_value=fake_verdict()))
        result=await app.scan(repo)
        app.ai_worker.llm_client.complete.assert_awaited_once()
        assert result.output.ai_tasks==1
        counts=await app.storage.read(lambda s:(s.count('incidents'),s.count('outbox_messages')))
        assert counts == (0,0), f'Negative final verdict leaves incidents/outbox: {counts}'


@pytest.mark.asyncio
async def test_targeted_ai_scan_must_upgrade_previous_local_candidate(tmp_path):
    """Targeted-пересканирование с AI должно реально вызывать LLM."""
    repo=tmp_path/'repo'
    sha=make_repo(repo)
    db=tmp_path/'upgrade.db'
    key=secrets.token_bytes(32)
    async with App(db,key) as app:
        await app.scan(repo)
    async with App(db,key,ai_enabled=True,use_fake_llm=True) as app:
        app.ai_worker.llm_client=SimpleNamespace(complete=AsyncMock(return_value=fake_verdict()))
        result=await app.scan(repo,ScanTaskPayload(mode=ScanMode.TARGETED,target_shas=[sha],ai_enabled=True))
        assert result.output.commits_done==1
        assert result.output.ai_tasks==1, 'Commit was reprocessed, but no AI task was created'


@pytest.mark.asyncio
async def test_new_prompt_verdict_must_update_existing_incident():
    """Две реальные AI-задачи разных версий должны применять оба вердикта."""
    async with SqliteStorage(':memory:') as s:
        c,f,t=await seed(s)
        client=client_with_response(response(severity='HIGH'))
        await worker(s,f,client).run_once()
        first=await s.get_ai_verdict_by_task(t.id)
        t2=await f.enqueue(Task.ai_validation(repository_id=c.repository_id,payload=AiValidationPayload(c.id,2)))
        client2=client_with_response(response(severity='CRITICAL'))
        await worker(s,f,client2).run_once()
        second=await s.get_ai_verdict_by_task(t2.id)
        assert second is not None and second.id != first.id
        result=await s.get_task_result(t2.id)
        inc=await s.read(lambda session:session.get_incident(result.output.incident_id))
        assert (inc.severity,inc.last_verdict_id)==(Severity.CRITICAL,second.id), 'New verdict was saved but not applied'


@pytest.mark.asyncio
async def test_stale_worker_must_not_commit_after_real_reclaim():
    """Аренду перевыдают публичные reap_expired/claim, без подмены статусов."""
    async with SqliteStorage(':memory:') as s:
        c,_,t=await seed(s)
        clock=utc_now()-timedelta(seconds=10)
        f=SqliteFeed(s,clock=lambda:clock)
        old=await f.claim([TaskKind.AI_VALIDATION],'owner-a',1)
        clock+=timedelta(seconds=2)
        assert await f.reap_expired()==1
        clock+=timedelta(seconds=2)
        new=await f.claim([TaskKind.AI_VALIDATION],'owner-b',30)
        assert new.id==old.id and new.attempts==2 and new.lease_owner=='owner-b'
        with pytest.raises(TaskLeaseLostError):
            await s.ai_validation_done(old,verdict(c,t),lambda session,v:None,
                lambda v,i:TaskResult(task_id=t.id,outcome=Outcome.OK,output=AiValidationOutput(v.id)),
                worker_id='owner-a')


@pytest.mark.asyncio
async def test_same_worker_stale_attempt_must_not_commit_after_reclaim():
    """Повторная выдача тому же worker должна отбрасывать старый claim."""
    async with SqliteStorage(':memory:') as s:
        c, f, t = await seed(s)
        clock = utc_now() - timedelta(seconds=10)
        f = SqliteFeed(s, clock=lambda: clock)
        old = await f.claim([TaskKind.AI_VALIDATION], 'owner-a', 1)
        clock += timedelta(seconds=2)
        assert await f.reap_expired() == 1
        clock += timedelta(seconds=2)
        new = await f.claim([TaskKind.AI_VALIDATION], 'owner-a', 30)
        assert new.id == old.id and new.attempts == 2
        with pytest.raises(TaskLeaseLostError):
            await s.ai_validation_done(
                old,
                verdict(c, t),
                lambda session, v: None,
                lambda v, i: TaskResult(
                    task_id=t.id,
                    outcome=Outcome.OK,
                    output=AiValidationOutput(v.id),
                ),
                worker_id='owner-a',
            )


@pytest.mark.asyncio
async def test_negative_verdict_must_close_existing_local_incident():
    """Отрицательный AI-вердикт должен закрыть ранее открытый локальный инцидент."""
    async with SqliteStorage(':memory:') as s:
        c, f, t = await seed(s)
        c.score_total = 0.95
        manager = IncidentManager()
        incident = await s.run_in_transaction(
            lambda session: manager.process(session, c)
        )
        assert incident is not None and incident.id is not None
        client = client_with_response({**response(), 'is_secret': False})
        await worker(s, f, client).run_once()
        stored = await s.read(lambda session: session.get_incident(incident.id))
        assert stored is not None
        assert stored.status is IncidentStatus.FALSE_POSITIVE


@pytest.mark.asyncio
async def test_cancelled_task_must_not_crash_worker_via_heartbeat():
    """Отмена настоящей задачи во время LLM не должна пробрасывать LeaseLostError."""
    async with SqliteStorage(':memory:') as s:
        c,f,t=await seed(s)
        lost=asyncio.Event()
        original_heartbeat=f.heartbeat
        async def heartbeat(tid):
            try:
                await original_heartbeat(tid)
            except LeaseLostError:
                lost.set()
                raise
        f.heartbeat=heartbeat
        client=client_with_response(response())
        async def complete(c,p):
            assert await f.cancel(t.id)==1
            await asyncio.wait_for(lost.wait(),1)
            return verdict(c,t)
        client.complete=complete
        assert await worker(s,f,client,lease_s=.03).run_once()
        assert (await s.get_task(t.id)).status is TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_resume_real_ai_scan_without_worker_must_not_spin(tmp_path):
    """Возобновить запуск, прерванный между commit worker и AI worker."""
    repo=tmp_path/'repo'
    make_repo(repo)
    db=tmp_path/'resume.db'
    key=secrets.token_bytes(32)
    async with App(db,key,ai_enabled=True,use_fake_llm=True) as app:
        r=await app.storage.save_repository(Repository(location=str(repo.resolve()),name='review'))
        root=await app.orchestrator.start_scan(r.id,ScanTaskPayload(ai_enabled=True))
        assert await app.orchestrator.plan_once()
        assert await app.commit_worker.run_once()
        tasks=await app.storage.read(lambda s:s.list_run_tasks(root.id))
        assert any(t.kind is TaskKind.AI_VALIDATION and t.status is TaskStatus.QUEUED for t in tasks)
    async with App(db,key,poll_interval_s=.01) as app:
        try:
            await asyncio.wait_for(app.scan(repo),.3)
        except asyncio.TimeoutError:
            pytest.fail('Resume spins forever: queued AI task has no worker')
        except (RuntimeError,ValueError):
            pass


@pytest.mark.asyncio
async def test_boolean_confidence_must_not_create_incident():
    """Некорректное boolean confidence не должно становиться уверенностью 1.0."""
    async with SqliteStorage(':memory:') as s:
        c,f,t=await seed(s)
        client=client_with_response(response(confidence=True))
        await worker(s,f,client).run_once()
        stored=await s.get_task(t.id)
        counts=await s.read(lambda session:(session.count('ai_verdicts'),session.count('incidents')))
        assert counts==(0,0), f'Malformed confidence became persisted verdict/incident: {counts}, status={stored.status.name}'
