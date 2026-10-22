from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from feed import SqliteFeed
from git_collector import CommitInfo, GitCollectorError
from incident_manager import IncidentManager
from models import (
    CommitStatus,
    Outcome,
    Repository,
    ScanMode,
    ScanTaskPayload,
    TaskKind,
    TaskStatus,
)
from orchestrator import ScanOrchestrator
from preprocessor import Preprocessor
from static_analyzer import StaticAnalyzer
from storage import SqliteStorage
from workers import CommitWorker, WorkerSettings


class _Collector:
    def __init__(self) -> None:
        self.first = b"a" * 20
        self.second = b"b" * 20
        self.fail_sha: bytes | None = None

    async def list_commits(self, refs):
        return [
            CommitInfo(self.first, (), None),
            CommitInfo(self.second, (self.first,), None),
        ]

    async def get_diff(self, sha):
        if sha == self.fail_sha:
            raise GitCollectorError("diff unavailable")
        if False:
            yield


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = SqliteStorage(Path(self.tmp.name) / "orchestrator.db")
        await self.storage.initialize()
        self.repo = await self.storage.save_repository(Repository("repo", "repo"))
        self.feed = SqliteFeed(self.storage)
        self.collector = _Collector()
        self.orchestrator = ScanOrchestrator(
            feed=self.feed,
            storage=self.storage,
            collector_for=lambda location: self.collector,
        )
        self.worker = CommitWorker(
            feed=self.feed,
            storage=self.storage,
            collector_for=lambda location: self.collector,
            preprocessor=Preprocessor(b"test-fingerprint-key"),
            analyzer=StaticAnalyzer(),
            incident_manager=IncidentManager(),
            settings=WorkerSettings("commit-worker", (TaskKind.COMMIT_ANALYSIS,)),
        )

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self.tmp.cleanup()

    async def test_plan_finalize_and_empty_repeat(self) -> None:
        first = await self.orchestrator.start_scan(
            self.repo.id, ScanTaskPayload(batch_size=1, ai_enabled=False)
        )
        self.assertTrue(await self.orchestrator.plan_once())
        root = await self.storage.get_task(first.id)
        self.assertEqual(root.status, TaskStatus.WAITING_CHILDREN)
        self.assertEqual(root.payload.mode, ScanMode.FULL)
        self.assertEqual(root.payload.planned_commits_total, 2)
        self.assertEqual(root.payload.planned_commits_new, 2)
        self.assertIsNone(await self.orchestrator.finalize(first.id))
        self.assertTrue(await self.worker.run_once())
        self.assertIsNone(await self.orchestrator.finalize(first.id))
        self.assertTrue(await self.worker.run_once())
        self.assertEqual(await self.orchestrator.finalize_ready(), [first.id])
        result = await self.storage.get_task_result(first.id)
        self.assertEqual(result.outcome, Outcome.OK)
        self.assertEqual(result.output.commits_done, 2)
        self.assertEqual(result.output.commits_failed, 0)
        self.assertEqual(len(await self.storage.read(
            lambda session: session.list_run_tasks(first.id)
        )), 2)
        repository = await self.storage.get_repository(self.repo.id)
        self.assertEqual(repository.last_scan_task_id, first.id)

        repeat = await self.orchestrator.start_scan(
            self.repo.id, ScanTaskPayload(ai_enabled=False)
        )
        self.assertTrue(await self.orchestrator.plan_once())
        repeated = await self.storage.get_task_result(repeat.id)
        self.assertEqual(repeated.outcome, Outcome.OK)
        self.assertEqual(repeated.output.mode, ScanMode.INCREMENTAL)
        self.assertEqual(repeated.output.commits_new, 0)
        self.assertEqual(await self.storage.read(
            lambda session: session.count("commits")
        ), 2)

        targeted = await self.orchestrator.start_scan(
            self.repo.id,
            ScanTaskPayload(
                mode=ScanMode.TARGETED,
                target_shas=[self.collector.first.hex()],
                ai_enabled=False,
            ),
        )
        self.assertTrue(await self.orchestrator.plan_once())
        targeted_children = await self.storage.read(
            lambda session: session.list_run_tasks(targeted.id)
        )
        self.assertEqual(len(targeted_children), 1)
        self.assertEqual(len(targeted_children[0].payload.commit_ids), 1)
        await self.worker.run_once()
        targeted_summary = await self.orchestrator.finalize(targeted.id)
        self.assertEqual(targeted_summary.mode, ScanMode.TARGETED)
        self.assertEqual(targeted_summary.commits_done, 1)

    async def test_failed_commit_can_be_scheduled_in_new_run(self) -> None:
        self.collector.fail_sha = self.collector.second
        first = await self.orchestrator.start_scan(
            self.repo.id, ScanTaskPayload(batch_size=2, ai_enabled=False)
        )
        await self.orchestrator.plan_once()
        first_children = await self.storage.read(
            lambda session: session.list_run_tasks(first.id)
        )
        await self.worker.run_once()
        summary = await self.orchestrator.finalize(first.id)
        self.assertEqual(summary.commits_failed, 1)
        self.assertEqual((await self.storage.get_task_result(first.id)).outcome, Outcome.PARTIAL)

        self.collector.fail_sha = None
        second = await self.orchestrator.start_scan(
            self.repo.id, ScanTaskPayload(batch_size=2, ai_enabled=False)
        )
        await self.orchestrator.plan_once()
        second_children = await self.storage.read(
            lambda session: session.list_run_tasks(second.id)
        )
        self.assertEqual(len(second_children), 1)
        self.assertNotEqual(first_children[0].id, second_children[0].id)
        self.assertNotEqual(first_children[0].idempotency_key, second_children[0].idempotency_key)
        await self.worker.run_once()
        summary = await self.orchestrator.finalize(second.id)
        self.assertEqual(summary.commits_new, 1)
        self.assertEqual(summary.commits_done, 1)
        commit = await self.storage.get_commit(second_children[0].payload.commit_ids[0])
        self.assertEqual(commit.status, CommitStatus.PROCESSED)


if __name__ == "__main__":
    unittest.main()
