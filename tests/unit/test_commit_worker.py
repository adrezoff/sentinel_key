from __future__ import annotations

import secrets
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from feed import SqliteFeed
from git_collector import (
    DiffHunk,
    DiffLine,
    DiffLineKind,
    FileChangeKind,
    FileDiff,
    GitCollectorError,
)
from incident_manager import IncidentManager
from models import (
    Commit,
    CommitAnalysisPayload,
    CommitStatus,
    ErrorCode,
    Outcome,
    Repository,
    Route,
    Task,
    TaskKind,
    TaskStatus,
)
from preprocessor import Preprocessor
from static_analyzer import StaticAnalyzer
from storage import SqliteStorage
from workers import CommitWorker, WorkerSettings


class _Collector:
    def __init__(self, diffs: dict[bytes, FileDiff | None]) -> None:
        self.diffs = diffs

    async def get_diff(self, sha: bytes):
        diff = self.diffs[sha]
        if diff is None:
            raise GitCollectorError("diff unavailable")
        yield diff


class CommitWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = SqliteStorage(Path(self.tmp.name) / "worker.db")
        await self.storage.initialize()
        self.feed = SqliteFeed(self.storage)
        self.repository = await self.storage.save_repository(
            Repository(location="example-repo", name="example-repo")
        )
        self.first = await self.storage.save_commit(
            Commit(repository_id=self.repository.id, sha=b"a" * 20)
        )
        self.second = await self.storage.save_commit(
            Commit(repository_id=self.repository.id, sha=b"b" * 20)
        )
        generated_value = secrets.token_urlsafe(32)
        self.diff = FileDiff(
            path=".env",
            change_kind=FileChangeKind.ADDED,
            hunks=(
                DiffHunk(
                    old_start=0,
                    old_count=0,
                    new_start=1,
                    new_count=1,
                    lines=(
                        DiffLine(
                            kind=DiffLineKind.ADD,
                            text=f"DATABASE_PASSWORD={generated_value}",
                            old_lineno=None,
                            new_lineno=1,
                        ),
                    ),
                ),
            ),
        )

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self.tmp.cleanup()

    def worker(self, diffs: dict[bytes, FileDiff | None]) -> CommitWorker:
        return CommitWorker(
            feed=self.feed,
            storage=self.storage,
            collector_for=lambda location: _Collector(diffs),
            preprocessor=Preprocessor(b"test-fingerprint-key"),
            analyzer=StaticAnalyzer(),
            incident_manager=IncidentManager(),
            settings=WorkerSettings("commit-worker", (TaskKind.COMMIT_ANALYSIS,)),
        )

    async def enqueue(self, commit_ids: list[int]) -> Task:
        return await self.feed.enqueue(
            Task.commit_analysis(
                repository_id=self.repository.id,
                payload=CommitAnalysisPayload(commit_ids=commit_ids),
            )
        )

    async def test_success_persists_masked_candidate_and_result(self) -> None:
        task = await self.enqueue([self.first.id])
        worker = self.worker({self.first.sha: self.diff})
        self.assertTrue(await worker.run_once())
        self.assertFalse(await worker.run_once())

        stored_task = await self.storage.get_task(task.id)
        stored_commit = await self.storage.get_commit(self.first.id)
        result = await self.storage.get_task_result(task.id)
        self.assertEqual(stored_task.status, TaskStatus.SUCCEEDED, stored_task.error)
        self.assertEqual(stored_commit.status, CommitStatus.PROCESSED)
        self.assertEqual(result.outcome, Outcome.OK)
        self.assertEqual(result.output.commit_ids_done, [self.first.id])
        self.assertEqual(result.output.ai_task_ids, [])
        self.assertGreaterEqual(result.output.candidates_total, 1)
        count = await self.storage.read(lambda session: session.count("candidates"))
        self.assertEqual(count, result.output.candidates_total)
        candidate = await self.storage.get_candidate(1)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.route, Route.SUSPICIOUS)
        self.assertNotIn("DATABASE_PASSWORD=", candidate.value_masked)

    async def test_git_error_only_fails_its_commit(self) -> None:
        task = await self.enqueue([self.first.id, self.second.id])
        worker = self.worker({self.first.sha: None, self.second.sha: self.diff})
        self.assertTrue(await worker.run_once())

        result = await self.storage.get_task_result(task.id)
        failed = await self.storage.get_commit(self.first.id)
        processed = await self.storage.get_commit(self.second.id)
        self.assertIsNotNone(result, (await self.storage.get_task(task.id)).error)
        self.assertEqual(result.outcome, Outcome.PARTIAL)
        self.assertEqual(result.output.commit_ids_failed, [self.first.id])
        self.assertEqual(result.output.commit_ids_done, [self.second.id])
        self.assertEqual(failed.status, CommitStatus.FAILED)
        self.assertEqual(failed.error_code, ErrorCode.GIT_ERROR)
        self.assertEqual(processed.status, CommitStatus.PROCESSED)


if __name__ == "__main__":
    unittest.main()
