from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from feed import LeaseLostError, SqliteFeed
from models import (
    CommitAnalysisPayload,
    ErrorCode,
    Outcome,
    Repository,
    ScanTaskPayload,
    Task,
    TaskError,
    TaskKind,
    TaskResult,
    TaskStatus,
)
from storage import SqliteStorage


NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class SqliteFeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        database = Path(self._tmpdir.name) / "feed.db"
        self.storage = SqliteStorage(database)
        await self.storage.initialize()
        repository = await self.storage.save_repository(
            Repository(location="repo", name="repo", created_at=NOW)
        )
        self.repository_id = repository.id
        self.clock = _Clock(NOW)
        self.feed = SqliteFeed(
            self.storage,
            heartbeat_lease_s=30,
            clock=self.clock,
        )

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmpdir.cleanup()

    def _scan(self, nonce: bytes = b"0123456789abcdef") -> Task:
        return Task.repository_scan(
            repository_id=self.repository_id,
            payload=ScanTaskPayload(),
            nonce=nonce,
            created_at=NOW,
        )

    def _commit_task(self, commit_id: int, **kwargs: object) -> Task:
        return Task.commit_analysis(
            repository_id=self.repository_id,
            payload=CommitAnalysisPayload(commit_ids=[commit_id]),
            created_at=NOW,
            **kwargs,
        )

    async def test_enqueue_is_idempotent_and_claim_sets_lease(self) -> None:
        task = self._scan()
        first = await self.feed.enqueue(task)
        second = await self.feed.enqueue(self._scan())
        self.assertEqual(first.id, second.id)

        claimed = await self.feed.claim(
            [TaskKind.REPOSITORY_SCAN], "worker-1", lease_s=10
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, first.id)
        self.assertEqual(claimed.status, TaskStatus.RUNNING)
        self.assertEqual(claimed.attempts, 1)
        self.assertEqual(claimed.lease_owner, "worker-1")
        self.assertEqual(claimed.lease_expires_at, NOW + timedelta(seconds=10))
        self.assertIsNone(
            await self.feed.claim([TaskKind.REPOSITORY_SCAN], "worker-2", 10)
        )

    async def test_claim_respects_priority_kind_and_not_before(self) -> None:
        delayed = self._commit_task(
            1,
            priority=1,
            not_before=NOW + timedelta(seconds=5),
        )
        ready = self._commit_task(2, priority=20)
        await self.feed.enqueue(delayed)
        await self.feed.enqueue(ready)

        self.assertIsNone(
            await self.feed.claim([TaskKind.REPOSITORY_SCAN], "scan", 10)
        )
        claimed = await self.feed.claim([TaskKind.COMMIT_ANALYSIS], "commit", 10)
        self.assertEqual(claimed.id, ready.id)

    async def test_heartbeat_extends_active_lease(self) -> None:
        task = await self.feed.enqueue(self._commit_task(1))
        claimed = await self.feed.claim([TaskKind.COMMIT_ANALYSIS], "worker", 10)
        self.clock.value += timedelta(seconds=5)
        await self.feed.heartbeat(claimed.id)
        stored = await self.storage.get_task(task.id)
        self.assertEqual(
            stored.lease_expires_at,
            self.clock.value + timedelta(seconds=30),
        )

        self.clock.value = stored.lease_expires_at
        with self.assertRaises(LeaseLostError):
            await self.feed.heartbeat(task.id)

    async def test_complete_persists_result_and_side_effects_atomically(self) -> None:
        await self.feed.enqueue(self._commit_task(1))
        task = await self.feed.claim([TaskKind.COMMIT_ANALYSIS], "worker", 10)
        result = TaskResult(task_id=task.id, outcome=Outcome.OK, created_at=NOW)

        await self.feed.complete(
            task,
            result,
            lambda session: session.save_repository(
                Repository(location="side-effect", name="side-effect", created_at=NOW)
            ),
        )

        stored = await self.storage.get_task(task.id)
        self.assertEqual(stored.status, TaskStatus.SUCCEEDED)
        self.assertIsNone(stored.lease_owner)
        self.assertEqual(await self.storage.get_task_result(task.id), result)
        count = await self.storage.read(lambda session: session.count("repositories"))
        self.assertEqual(count, 2)

    async def test_complete_rolls_back_when_side_effect_fails(self) -> None:
        await self.feed.enqueue(self._commit_task(1))
        task = await self.feed.claim([TaskKind.COMMIT_ANALYSIS], "worker", 10)
        result = TaskResult(task_id=task.id, outcome=Outcome.OK, created_at=NOW)

        def fail_after_write(session: object) -> None:
            session.save_repository(  # type: ignore[attr-defined]
                Repository(location="rolled-back", name="rolled-back", created_at=NOW)
            )
            raise RuntimeError("side effect failed")

        with self.assertRaisesRegex(RuntimeError, "side effect failed"):
            await self.feed.complete(task, result, fail_after_write)

        stored = await self.storage.get_task(task.id)
        self.assertEqual(stored.status, TaskStatus.RUNNING)
        self.assertIsNone(await self.storage.get_task_result(task.id))
        count = await self.storage.read(lambda session: session.count("repositories"))
        self.assertEqual(count, 1)

    async def test_fail_retries_then_records_terminal_result(self) -> None:
        task = self._commit_task(1, max_attempts=2)
        await self.feed.enqueue(task)
        claimed = await self.feed.claim([TaskKind.COMMIT_ANALYSIS], "worker", 10)
        error = TaskError(ErrorCode.TIMEOUT, "operation timed out", retryable=True)

        await self.feed.fail(claimed, error)
        stored = await self.storage.get_task(claimed.id)
        self.assertEqual(stored.status, TaskStatus.QUEUED)
        self.assertEqual(stored.not_before, NOW + timedelta(seconds=1))
        self.assertIsNone(await self.storage.get_task_result(claimed.id))

        self.clock.value += timedelta(seconds=1)
        claimed = await self.feed.claim([TaskKind.COMMIT_ANALYSIS], "worker", 10)
        await self.feed.fail(claimed, error)
        stored = await self.storage.get_task(claimed.id)
        self.assertEqual(stored.status, TaskStatus.FAILED)
        result = await self.storage.get_task_result(claimed.id)
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertEqual(result.error_code, ErrorCode.TIMEOUT)

    async def test_reap_expired_and_cancel_run(self) -> None:
        await self.feed.enqueue(self._commit_task(1))
        expired = await self.feed.claim([TaskKind.COMMIT_ANALYSIS], "worker", 1)
        self.clock.value += timedelta(seconds=2)
        self.assertEqual(await self.feed.reap_expired(), 1)
        stored = await self.storage.get_task(expired.id)
        self.assertEqual(stored.status, TaskStatus.QUEUED)
        self.assertEqual(stored.error_code, ErrorCode.LEASE_EXPIRED)

        root = await self.feed.enqueue(self._scan())
        child = await self.feed.enqueue(self._commit_task(2, root_task_id=root.id))
        self.assertEqual(await self.feed.cancel(root.id), 2)
        self.assertEqual((await self.storage.get_task(root.id)).status, TaskStatus.CANCELLED)
        self.assertEqual((await self.storage.get_task(child.id)).status, TaskStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
