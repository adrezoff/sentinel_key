from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from models import (
    Candidate,
    Commit,
    DetectorFlags,
    Incident,
    IncidentEvent,
    IncidentEventKind,
    NotifyChannel,
    Occurrence,
    Outcome,
    OutboxMessage,
    Repository,
    Route,
    ScanSummary,
    SecretType,
    Severity,
    Task,
    TaskResult,
)
from models.task.scan_task_payload import ScanMode, ScanTaskPayload
from storage import SqliteStorage


NOW = datetime(2026, 10, 22, 12, 0, 0, 123000, tzinfo=timezone.utc)


class SqliteStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.database = Path(self._tmpdir.name) / "nested" / "sentinel.db"
        self.storage = SqliteStorage(self.database)
        await self.storage.initialize()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmpdir.cleanup()

    async def test_initialize_is_idempotent_and_enables_wal(self) -> None:
        await self.storage.initialize()
        self.assertEqual(await self.storage.schema_version(), 1)
        self.assertEqual(await self.storage.journal_mode(), "wal")
        self.assertTrue(self.database.exists())

    async def test_repository_commit_task_and_result_roundtrip(self) -> None:
        repository = Repository(
            location="C:/repos/example",
            name="example",
            default_ref="main",
            created_at=NOW,
        )
        stored_repository = await self.storage.save_repository(repository)
        self.assertEqual(repository.id, stored_repository.id)

        commit = Commit(
            repository_id=repository.id,
            sha=b"a" * 20,
            committed_at=NOW,
        )
        stored_commit = await self.storage.save_commit(commit)
        self.assertEqual(stored_commit.sha, commit.sha)
        self.assertEqual(stored_commit.committed_at, NOW)

        task = Task.repository_scan(
            repository_id=repository.id,
            payload=ScanTaskPayload(mode=ScanMode.FULL, ai_enabled=False),
            nonce=b"0123456789abcdef",
            created_at=NOW,
        )
        stored_task = await self.storage.save_task(task)
        duplicate = await self.storage.save_task(task)
        self.assertEqual(stored_task.id, duplicate.id)
        loaded_task = await self.storage.get_task(task.id)
        self.assertEqual(loaded_task.payload, task.payload)

        result = TaskResult(
            task_id=task.id,
            outcome=Outcome.OK,
            output=ScanSummary(
                mode=ScanMode.FULL,
                commits_total=1,
                commits_new=1,
                commits_done=1,
                commits_failed=0,
                candidates=0,
                ai_tasks=0,
                ai_failed=0,
                incidents_opened=0,
                incidents_updated=0,
            ),
            metrics={"duration_s": 0.5},
            created_at=NOW,
        )
        await self.storage.save_task_result(result)
        self.assertEqual(await self.storage.get_task_result(task.id), result)

    async def test_candidate_and_incident_graph_is_atomic(self) -> None:
        repository = Repository(location="repo", name="repo", created_at=NOW)
        await self.storage.save_repository(repository)
        commit = Commit(repository_id=repository.id, sha=b"b" * 20, committed_at=NOW)
        await self.storage.save_commit(commit)
        candidate = Candidate(
            repository_id=repository.id,
            commit_id=commit.id,
            file_path="config/settings.py",
            line_number=7,
            key_name="API_KEY",
            secret_type=SecretType.API_KEY,
            value_masked="abcd...wxyz",
            value_fingerprint=b"f" * 32,
            entropy=4.1,
            regex_rules=["generic-api-key"],
            detector_flags=DetectorFlags.REGEX | DetectorFlags.ENTROPY,
            source_line_masked="API_KEY = 'abcd...wxyz'",
            context_masked="",
            is_test_context=False,
            score_regex=0.5,
            score_entropy=0.2,
            score_context=0.2,
            score_token_type=0.1,
            score_total=1.0,
            route=Route.SUSPICIOUS,
            created_at=NOW,
        )
        await self.storage.save_candidate(candidate)

        incident = Incident(
            repository_id=repository.id,
            fingerprint=candidate.value_fingerprint,
            secret_type=candidate.secret_type,
            severity=Severity.HIGH,
            confidence=0.95,
            detector_flags=candidate.detector_flags,
            evidence_masked=candidate.value_masked,
            reason="trusted format",
            recommendation="rotate",
            first_commit_id=commit.id,
            created_at=NOW,
            updated_at=NOW,
        )

        def persist_graph(session):
            stored_incident = session.save_incident(incident)
            session.save_occurrence(
                Occurrence(
                    incident_id=stored_incident.id,
                    candidate_id=candidate.id,
                    seen_at=NOW,
                )
            )
            session.save_incident_event(
                IncidentEvent(
                    incident_id=stored_incident.id,
                    kind=IncidentEventKind.OPENED,
                    actor="system",
                    created_at=NOW,
                )
            )
            message = session.save_outbox_message(
                OutboxMessage(
                    incident_id=stored_incident.id,
                    channel=NotifyChannel.WEBHOOK,
                    payload={"evidence": candidate.value_masked},
                    created_at=NOW,
                )
            )
            return stored_incident.id, message.id

        incident_id, message_id = await self.storage.run_in_transaction(persist_graph)
        loaded = await self.storage.read(lambda session: session.get_incident(incident_id))
        self.assertEqual(loaded.fingerprint, candidate.value_fingerprint)
        graph = await self.storage.read(
            lambda session: (
                session.list_occurrences(incident_id),
                session.list_incident_events(incident_id),
                session.get_outbox_message(message_id),
            )
        )
        self.assertEqual(graph[0][0].candidate_id, candidate.id)
        self.assertEqual(graph[1][0].kind, IncidentEventKind.OPENED)
        self.assertEqual(graph[2].payload, {"evidence": candidate.value_masked})
        counts = await self.storage.read(
            lambda session: tuple(
                session.count(table)
                for table in ("incidents", "occurrences", "incident_events", "outbox_messages")
            )
        )
        self.assertEqual(counts, (1, 1, 1, 1))

    async def test_failed_composite_operation_rolls_back(self) -> None:
        def fail(session):
            session.save_repository(Repository(location="rollback", name="rollback"))
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            await self.storage.run_in_transaction(fail)
        count = await self.storage.read(lambda session: session.count("repositories"))
        self.assertEqual(count, 0)

    async def test_schema_has_no_raw_secret_columns(self) -> None:
        await self.storage.close()
        connection = sqlite3.connect(self.database)
        try:
            columns = {
                row[1]
                for table in ("candidates", "incidents", "outbox_messages")
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
        finally:
            connection.close()
        self.assertNotIn("raw_value", columns)
        self.assertNotIn("source_line_raw", columns)


if __name__ == "__main__":
    unittest.main()
