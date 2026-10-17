from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from models import (
    CommitAnalysisPayload,
    InvalidTransition,
    ScanTaskPayload,
    Task,
    TaskError,
    TaskKind,
    TaskStatus,
)
from models.task.task_error import ErrorCode
from models.task.task_kinds import parse_payload, spec_for


NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


class TaskDefaultsTests(unittest.TestCase):
    def test_scan_defaults(self) -> None:
        task = Task.repository_scan(
            repository_id=1,
            payload=ScanTaskPayload(),
            nonce=b"nonce-16-bytes!!",
        )
        self.assertEqual(task.kind, TaskKind.REPOSITORY_SCAN)
        self.assertEqual(task.status, TaskStatus.QUEUED)
        self.assertEqual(task.priority, 10)
        self.assertEqual(task.max_attempts, 3)
        self.assertEqual(len(task.idempotency_key), 16)
        self.assertIsNone(task.id)
        self.assertEqual(task.run_id, task.id)

    def test_ai_validation_kind_reserved_not_registered(self) -> None:
        self.assertEqual(int(TaskKind.AI_VALIDATION), 3)
        with self.assertRaises(KeyError):
            spec_for(TaskKind.AI_VALIDATION)

    def test_commit_idempotency_ignores_order(self) -> None:
        a = Task.commit_analysis(
            repository_id=1,
            payload=CommitAnalysisPayload(commit_ids=[3, 1, 2]),
        )
        b = Task.commit_analysis(
            repository_id=1,
            payload=CommitAnalysisPayload(commit_ids=[1, 2, 3]),
        )
        self.assertEqual(a.idempotency_key, b.idempotency_key)


class TaskTransitionTests(unittest.TestCase):
    def _queued(self) -> Task:
        return Task.repository_scan(
            repository_id=1,
            payload=ScanTaskPayload(),
            nonce=b"0123456789abcdef",
        )

    def test_happy_path_with_waiting_children(self) -> None:
        task = self._queued()
        task.transition(TaskStatus.RUNNING, NOW)
        self.assertEqual(task.attempts, 1)
        self.assertEqual(task.started_at, NOW)
        task.lease_owner = "w1"
        task.lease_expires_at = NOW + timedelta(seconds=30)
        task.transition(TaskStatus.WAITING_CHILDREN, NOW)
        self.assertIsNone(task.lease_owner)
        self.assertIsNone(task.lease_expires_at)
        task.transition(TaskStatus.SUCCEEDED, NOW)
        self.assertEqual(task.finished_at, NOW)
        self.assertTrue(task.is_terminal)

    def test_waiting_children_only_for_scan(self) -> None:
        task = Task.commit_analysis(
            repository_id=1,
            payload=CommitAnalysisPayload(commit_ids=[1]),
        )
        task.transition(TaskStatus.RUNNING, NOW)
        with self.assertRaises(InvalidTransition):
            task.transition(TaskStatus.WAITING_CHILDREN, NOW)

    def test_illegal_transition(self) -> None:
        task = self._queued()
        with self.assertRaises(InvalidTransition):
            task.transition(TaskStatus.SUCCEEDED, NOW)

    def test_retry_backoff(self) -> None:
        task = self._queued()
        task.transition(TaskStatus.RUNNING, NOW)
        error = TaskError(ErrorCode.TIMEOUT, "timed out", retryable=True)
        task.schedule_retry(error, NOW)
        self.assertEqual(task.status, TaskStatus.QUEUED)
        self.assertEqual(task.not_before, NOW + timedelta(seconds=1))
        task.transition(TaskStatus.RUNNING, NOW)
        task.schedule_retry(error, NOW)
        self.assertEqual(task.not_before, NOW + timedelta(seconds=2))

    def test_retry_exhausted_goes_failed(self) -> None:
        task = self._queued()
        task.max_attempts = 1
        task.transition(TaskStatus.RUNNING, NOW)
        task.schedule_retry(TaskError(ErrorCode.TIMEOUT, "x", retryable=True), NOW)
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.error_code, ErrorCode.TIMEOUT)

    def test_non_retryable_fails(self) -> None:
        task = self._queued()
        task.transition(TaskStatus.RUNNING, NOW)
        task.schedule_retry(TaskError(ErrorCode.MASKING_VIOLATION, "x", retryable=False), NOW)
        self.assertEqual(task.status, TaskStatus.FAILED)

    def test_payload_codec_roundtrip(self) -> None:
        from models.task.scan_task_payload import ScanMode, Trigger

        original = ScanTaskPayload(
            mode=ScanMode.TARGETED,
            trigger=Trigger.WEBHOOK,
            target_shas=["abc"],
            ai_enabled=False,
        )
        data = original.to_json_dict()
        restored = parse_payload(TaskKind.REPOSITORY_SCAN, data)
        self.assertEqual(restored, original)
        self.assertEqual(spec_for(TaskKind.REPOSITORY_SCAN).default_priority, 10)


if __name__ == "__main__":
    unittest.main()
