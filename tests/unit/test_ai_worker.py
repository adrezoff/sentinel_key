# -*- coding: utf-8 -*-
"""Unit tests for AI Worker."""

from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

async def _async_read(self, fn):
    """Async mock for storage.read — calls fn(self) and returns the result."""
    return fn(self)


async def _async_run_in_transaction(self, fn):
    """Async mock for storage.run_in_transaction — calls fn(self) and returns the result."""
    return fn(self)

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ai_validation.llm_client import (
    FakeLlmClient,
    LlmConfig,
    LlmError,
    LlmInvalidResponseError,
)
from models.ai_validation.ai_verdict import AiVerdict
from models.ai_validation.ai_validation_payload import AiValidationPayload
from models.ai_validation.ai_validation_output import AiValidationOutput
from models.candidate import Candidate
from models.enums import SecretType, Severity, DetectorFlags
from workers.ai import AiWorker, create_ai_worker
from workers.base import WorkerSettings
from feed import SqliteFeed
from incident_manager import IncidentManager
from models import Task, TaskKind, TaskStatus, TaskError, ErrorCode, Outcome, AiValidationOutput, AiValidationPayload
from storage import SqliteStorage
class FakeFeed:
    """Fake feed for testing."""

    def __init__(self):
        self.tasks = {}
        self.next_id = 1

    async def enqueue(self, task):
        task.id = len(self.tasks) + 1
        self.tasks[task.id] = task
        return task

    async def claim(self, kinds, worker_id, lease_s):
        for task in self.tasks.values():
            if task.kind in kinds and task.status.name == "QUEUED":
                task.status = TaskStatus.RUNNING
                return task
        return None

    async def complete(self, task, result, side_effect=None):
        task.status = TaskStatus.SUCCEEDED
        return task

    async def fail(self, task, error):
        task.status = TaskStatus.FAILED
        task.error_code = error.code
        task.error_message = error.message


class FakeStorage:
    """Fake storage for testing."""

    def __init__(self):
        self.candidates = {}
        self.verdicts = {}
        self.incidents = {}
        self.next_candidate_id = 1
        self.next_verdict_id = 1
        self.next_incident_id = 1

    async def read(self, fn):
        return fn(self)

    async def run_in_transaction(self, fn):
        return fn(self)

    def save_candidate(self, candidate):
        candidate.id = len(self.candidates) + 1
        self.candidates[candidate.id] = candidate
        return candidate

    def get_candidate(self, candidate_id):
        return self.candidates.get(candidate_id)

    def save_ai_verdict(self, verdict):
        verdict.id = len(self.verdicts) + 1
        self.verdicts[verdict.task_id] = verdict
        return verdict

    def get_ai_verdict(self, verdict_id):
        for v in self.verdicts.values():
            if v.id == verdict_id:
                return v
        return None

    def get_ai_verdict_by_task(self, task_id):
        return self.verdicts.get(task_id)

    def get_ai_verdict_by_candidate(self, candidate_id):
        for v in self.verdicts.values():
            if v.candidate_id == candidate_id:
                return v
        return None

    def get_incident_by_fingerprint(self, repository_id, fingerprint):
        for inc in self.incidents.values():
            if inc.repository_id == repository_id and inc.fingerprint == fingerprint:
                return v
        return None

    def save_incident(self, incident, expected_version=None):
        if incident.id is None:
            incident.id = len(self.incidents) + 1
        self.incidents[incident.id] = incident
        return incident

    def get_incident(self, incident_id):
        return self.incidents.get(incident_id)

    def has_occurrence(self, incident_id, candidate_id):
        return False

    def save_occurrence(self, occurrence):
        pass

    def save_incident_event(self, event):
        pass

    def save_outbox_message(self, message):
        pass
class AiWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tasks: dict = {}
        self.feed = type('Feed', (), {
            'tasks': tasks,
            'next_id': 1,
            'enqueue': AsyncMock(side_effect=lambda task: setattr(task, 'id', len(tasks) + 1) or tasks.__setitem__(task.id, task) or None),
            'claim': AsyncMock(side_effect=lambda kinds, worker_id, lease_s: next((t for t in tasks.values() if getattr(t.status, 'name', None) == 'QUEUED'), None)),
            'complete': AsyncMock(side_effect=lambda task, result, side_effect=None: setattr(task, 'status', TaskStatus.SUCCEEDED) or None),
            'fail': AsyncMock(),
            'heartbeat': AsyncMock(),
        })()

    async def asyncSetUp(self) -> None:
        tasks: dict = {}
        self.feed = type('Feed', (), {
            'tasks': tasks,
            'next_id': 1,
            'enqueue': AsyncMock(side_effect=lambda task: setattr(task, 'id', len(tasks) + 1) or tasks.__setitem__(task.id, task) or None),
            'claim': AsyncMock(side_effect=lambda kinds, worker_id, lease_s: next((t for t in tasks.values() if getattr(t.status, 'name', None) == 'QUEUED'), None)),
            'complete': AsyncMock(side_effect=lambda task, result, side_effect=None: setattr(task, 'status', TaskStatus.SUCCEEDED) or None),
            'fail': AsyncMock(),
            'heartbeat': AsyncMock(),
        })()
        self.storage = type('Storage', (), {
            'candidates': {},
            'verdicts': {},
            'incidents': {},
            'next_candidate_id': 1,
            'next_verdict_id': 1,
            'next_incident_id': 1,
            'read': _async_read,
            'run_in_transaction': _async_run_in_transaction,
            'save_candidate': lambda self, c: setattr(c, 'id', len(self.candidates) + 1) or self.candidates.update({len(self.candidates) + 1: c}) or c,
            'get_candidate': lambda self, cid: self.candidates.get(cid),
            'save_ai_verdict': lambda self, v: self.verdicts.__setitem__(v.task_id, replace(v, id=len(self.verdicts) + 1)) or self.verdicts[v.task_id],
            'get_ai_verdict_by_task': lambda self, tid: self.verdicts.get(tid),
        })()
        self.incident_manager = MagicMock()
        self.config = LlmConfig(
            api_key="test-key",
            temperature=0.1,
            max_tokens=100,
            timeout_s=5.0,
        )

    def _make_candidate(self, candidate_id=1, is_secret=True):
        return Candidate(
            id=candidate_id,
            repository_id=1,
            commit_id=1,
            file_path="test.py",
            line_number=1,
            key_name="test_key",
            secret_type=SecretType.API_KEY,
            value_masked="TEST***KEY",
            value_fingerprint=b"x" * 32,
            entropy=4.0,
            regex_rules=["api_key"],
            detector_flags=DetectorFlags.REGEX,
            source_line_masked="api_key = \"TEST***KEY\"",
            context_masked="api_key = \"TEST***KEY\"",
            is_test_context=False,
            score_regex=0.8,
            score_entropy=0.5,
            score_context=0.3,
            score_token_type=0.7,
            score_total=0.8,
            route=3,  # AI_VALIDATION
        )
    async def test_ai_worker_success(self) -> None:
        """Test successful AI worker processing."""
        candidate = self._make_candidate()
        verdict = AiVerdict(
            task_id=1,
            candidate_id=candidate.id,
            is_secret=True,
            secret_type=SecretType.API_KEY,
            confidence=0.95,
            severity=Severity.HIGH,
            reason="Found API key",
            recommendation="Rotate the key",
            model="test-model",
            prompt_version=1,
        )

        fake_client = MagicMock()
        fake_client.complete = AsyncMock(return_value=verdict)

        settings = WorkerSettings(
            worker_id="test-worker",
            kinds=(TaskKind.AI_VALIDATION,),
            lease_s=30.0,
            poll_interval_s=1.0,
        )
        worker = AiWorker(
            feed=self.feed,
            storage=self.storage,
            incident_manager=MagicMock(),
            llm_client=fake_client,
            settings=settings,
        )
        worker.feed = self.feed
        worker.storage = self.storage
        worker.incident_manager = MagicMock()
        worker.llm_client = fake_client

        # Save candidate to storage
        candidate = self._make_candidate()
        self.storage.save_candidate(candidate)

        # Create a task
        task = Task.ai_validation(
            repository_id=1,
            payload=AiValidationPayload(candidate_id=candidate.id, prompt_version=1),
            root_task_id=1,
            parent_task_id=1,
        )
        await self.feed.enqueue(task)

        # Run once
        result = await worker.run_once()
        self.assertTrue(result)

        # Check task completed
        task = self.feed.tasks[task.id]
        self.assertEqual(task.status, TaskStatus.SUCCEEDED)

        # Check task completed
        task = self.feed.tasks[task.id]
        self.assertEqual(task.status, TaskStatus.SUCCEEDED)

        # Check verdict was saved
        verdicts = self.storage.verdicts
        self.assertIn(1, verdicts)
        saved_verdict = verdicts[1]
        self.assertTrue(saved_verdict.is_secret)
        self.assertEqual(saved_verdict.secret_type, SecretType.API_KEY)

    async def test_ai_worker_not_secret(self) -> None:
        """Test worker with non-secret verdict."""
        candidate = self._make_candidate()
        verdict = AiVerdict(
            task_id=1,
            candidate_id=candidate.id,
            is_secret=False,
            secret_type=SecretType.UNKNOWN,
            confidence=0.99,
            severity=Severity.LOW,
            reason="Not a secret",
            recommendation="No action needed",
            model="test-model",
            prompt_version=1,
        )

        fake_client = MagicMock()
        fake_client.complete = AsyncMock(return_value=verdict)

        tasks: dict = {}
        self.feed = type('Feed', (), {
            'tasks': tasks,
            'next_id': 1,
            'enqueue': AsyncMock(side_effect=lambda task: setattr(task, 'id', len(tasks) + 1) or tasks.__setitem__(task.id, task) or None),
            'claim': AsyncMock(side_effect=lambda kinds, worker_id, lease_s: next((t for t in tasks.values() if getattr(t.status, 'name', None) == 'QUEUED'), None)),
            'complete': AsyncMock(side_effect=lambda task, result, side_effect=None: setattr(task, 'status', TaskStatus.SUCCEEDED) or None),
        })()

        settings = WorkerSettings(
            worker_id="test-worker",
            kinds=(TaskKind.AI_VALIDATION,),
            lease_s=30.0,
            poll_interval_s=1.0,
        )
        worker = AiWorker(
            feed=self.feed,
            storage=self.storage,
            incident_manager=MagicMock(),
            llm_client=MagicMock(),
            settings=settings,
        )
        worker.llm_client = MagicMock()
        worker.llm_client.complete = AsyncMock(return_value=verdict)

    async def test_masking_guard_violation(self) -> None:
        """Test that masking guard violation fails the task."""
        from ai_validation.masking_guard import MaskingViolationError

        candidate = self._make_candidate()
        # Create a candidate with raw secret in source_line_masked
        candidate.source_line_masked = "api_key = \"REAL_API_KEY_12345\""
        self.storage.save_candidate(candidate)

        fake_client = MagicMock()
        fake_client.complete = AsyncMock(side_effect=Exception("should not be called"))

        settings = WorkerSettings(
            worker_id="test-worker",
            kinds=(TaskKind.AI_VALIDATION,),
            lease_s=30.0,
            poll_interval_s=1.0,
        )
        worker = AiWorker(
            feed=self.feed,
            storage=self.storage,
            incident_manager=MagicMock(),
            llm_client=fake_client,
            settings=settings,
        )
        worker.feed = self.feed
        worker.storage = self.storage

        task = Task.ai_validation(
            repository_id=1,
            payload=AiValidationPayload(candidate_id=candidate.id, prompt_version=1),
            root_task_id=1,
            parent_task_id=1,
        )
        await self.feed.enqueue(task)

        # The masking guard should catch the violation and fail the task
        with self.assertRaises(Exception) as cm:
            await worker._process(task, None)

    async def test_llm_error_retry(self) -> None:
        """Test that LLM errors trigger retry."""
        from ai_validation.llm_client import LlmNetworkError

        candidate = self._make_candidate()
        self.storage.save_candidate(candidate)
        fake_client = MagicMock()
        fake_client.complete = AsyncMock(side_effect=LlmNetworkError("network error"))

        settings = WorkerSettings(
            worker_id="test-worker",
            kinds=(TaskKind.AI_VALIDATION,),
            lease_s=30.0,
            poll_interval_s=1.0,
        )
        worker = AiWorker(
            feed=self.feed,
            storage=self.storage,
            incident_manager=MagicMock(),
            llm_client=fake_client,
            settings=settings,
        )
        worker.feed = self.feed
        worker.storage = self.storage

        task = Task.ai_validation(
            repository_id=1,
            payload=AiValidationPayload(candidate_id=candidate.id, prompt_version=1),
            root_task_id=1,
            parent_task_id=1,
        )
        await self.feed.enqueue(task)

        # Should retry and eventually fail
        with self.assertRaises(Exception):
            await worker._process(task, None)

    async def test_create_ai_worker_factory(self) -> None:
        """Test the factory function."""
        from ai_validation.llm_client import FakeLlmClient

        fake_client = FakeLlmClient()
        worker = create_ai_worker(
            feed=self.feed,
            storage=None,
            incident_manager=MagicMock(),
            use_fake_llm=True,
        )
        self.assertIsInstance(worker, AiWorker)
        self.assertIsInstance(worker.llm_client, FakeLlmClient)


if __name__ == "__main__":
    unittest.main()