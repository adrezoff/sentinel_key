"""Integration tests AI worker with a real temporary SQLite database."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from feed import SqliteFeed
from incident_manager import IncidentManager
from models import (
    AiValidationOutput,
    AiValidationPayload,
    Candidate,
    Commit,
    DetectorFlags,
    ErrorCode,
    Outcome,
    Repository,
    Route,
    SecretType,
    Severity,
    Task,
    TaskKind,
    TaskStatus,
)
from models.ai_validation.ai_verdict import AiVerdict
from storage import SqliteStorage
from workers.ai import AiWorker
from workers.base import WorkerSettings


class SqliteAiWorkerTests(unittest.IsolatedAsyncioTestCase):
    """Проверить сохранённые связи и атомарность AI-пути на настоящей SQLite."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.storage = SqliteStorage(Path(self._tmpdir.name) / "ai-worker.db")
        await self.storage.initialize()
        self.feed = SqliteFeed(self.storage)
        self.repository = await self.storage.save_repository(
            Repository(location="repo", name="repo")
        )
        self.commit = await self.storage.save_commit(
            Commit(repository_id=self.repository.id, sha=b"a" * 20)
        )

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmpdir.cleanup()

    def _candidate(self) -> Candidate:
        return Candidate(
            repository_id=self.repository.id,
            commit_id=self.commit.id,
            file_path="config.py",
            line_number=10,
            key_name="api_key",
            secret_type=SecretType.API_KEY,
            value_masked="test-***-key",
            value_fingerprint=b"x" * 32,
            entropy=4.0,
            detector_flags=DetectorFlags.REGEX,
            source_line_masked="api_key = 'test-***-key'",
            context_masked="api_key = 'test-***-key'",
            is_test_context=False,
            score_regex=0.9,
            score_entropy=0.8,
            score_context=0.7,
            score_token_type=0.8,
            score_total=0.9,
            route=Route.AI_VALIDATION,
        )

    def _verdict(self, candidate_id: int, task_id: int = 0) -> AiVerdict:
        return AiVerdict(
            task_id=task_id,
            candidate_id=candidate_id,
            is_secret=True,
            secret_type=SecretType.API_KEY,
            confidence=0.95,
            severity=Severity.HIGH,
            reason="confirmed secret",
            recommendation="rotate",
            model="fake-model",
            prompt_version=1,
        )

    def _worker(self, client: MagicMock) -> AiWorker:
        return AiWorker(
            feed=self.feed,
            storage=self.storage,
            incident_manager=IncidentManager(),
            llm_client=client,
            settings=WorkerSettings("ai-test", (TaskKind.AI_VALIDATION,), 30.0, 0.01),
        )

    async def _enqueue_candidate(self) -> tuple[Candidate, Task]:
        candidate = await self.storage.save_candidate(self._candidate())
        task = Task.ai_validation(
            repository_id=self.repository.id,
            payload=AiValidationPayload(candidate_id=candidate.id, prompt_version=1),
        )
        await self.feed.enqueue(task)
        return candidate, task

    async def test_success_persists_verdict_incident_outbox_and_result_atomically(self) -> None:
        """Все части успешного AI-результата связаны реальными FK и одним task result."""
        candidate, task = await self._enqueue_candidate()
        client = MagicMock()
        client.complete = AsyncMock(return_value=self._verdict(candidate.id))
        worker = self._worker(client)

        self.assertTrue(await worker.run_once())

        stored_task = await self.storage.get_task(task.id)
        result = await self.storage.get_task_result(task.id)
        verdict = await self.storage.get_ai_verdict_by_task(task.id)
        self.assertEqual(stored_task.status, TaskStatus.SUCCEEDED)
        self.assertEqual(result.outcome, Outcome.OK)
        self.assertIsInstance(result.output, AiValidationOutput)
        self.assertIsNotNone(verdict)
        self.assertEqual(result.output.verdict_id, verdict.id)
        self.assertIsNotNone(result.output.incident_id)
        self.assertEqual(verdict.candidate_id, candidate.id)
        self.assertEqual(verdict.task_id, task.id)
        counts = await self.storage.read(
            lambda session: tuple(
                session.count(table)
                for table in (
                    "ai_verdicts",
                    "incidents",
                    "occurrences",
                    "incident_events",
                    "outbox_messages",
                    "task_results",
                )
            )
        )
        self.assertEqual(counts, (1, 1, 1, 1, 1, 1))

    async def test_rollback_removes_verdict_when_incident_changes_fail(self) -> None:
        """Ошибка изменения инцидента откатывает вердикт и результат задачи.

        Задача остаётся в статусе RUNNING (claim был отдельной операцией);
        откат транзакции ai_validation_done удаляет вердикт и task_result.
        """
        candidate, task = await self._enqueue_candidate()
        claimed = await self.feed.claim(
            [TaskKind.AI_VALIDATION], "rollback-worker", 30.0
        )

        with self.assertRaisesRegex(RuntimeError, "incident failure"):
            await self.storage.ai_validation_done(
                claimed,
                self._verdict(candidate.id, claimed.id),
                lambda session, verdict: (_ for _ in ()).throw(RuntimeError("incident failure")),
                lambda verdict, incident: None,
            )

        self.assertIsNone(await self.storage.get_ai_verdict_by_task(task.id))
        self.assertIsNone(await self.storage.get_task_result(task.id))
        stored = await self.storage.get_task(task.id)
        # claim перевёл задачу в RUNNING; ai_validation_done не меняет статус при ошибке
        self.assertEqual(stored.status, TaskStatus.RUNNING)

    async def test_masking_violation_error_does_not_store_secret_fragment(self) -> None:
        """Ошибка masking guard хранит только безопасный код и сообщение."""
        candidate = await self.storage.save_candidate(self._candidate())
        secret = "AKIAIOSFODNN7EXAMPLE"
        original_read = self.storage.read

        async def read_with_unsafe_candidate(operation):
            stored = await original_read(operation)
            if isinstance(stored, Candidate):
                # value_masked используется в промпте, поэтому маскировка должна сработать
                stored.value_masked = secret
            return stored

        self.storage.read = read_with_unsafe_candidate
        task = Task.ai_validation(
            repository_id=self.repository.id,
            payload=AiValidationPayload(candidate_id=candidate.id, prompt_version=1),
        )
        await self.feed.enqueue(task)
        client = MagicMock()
        client.complete = AsyncMock(side_effect=AssertionError("LLM must not be called"))
        worker = self._worker(client)

        self.assertTrue(await worker.run_once())

        result = await self.storage.get_task_result(task.id)
        self.assertEqual(result.error_code, ErrorCode.MASKING_VIOLATION)
        self.assertNotIn(secret, result.error_message)
        self.assertNotIn("AKIAIOSF", result.error_message)
        client.complete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
