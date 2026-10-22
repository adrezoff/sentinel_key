"""Проверка локального решения и атомарного графа инцидента в SQLite."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from incident_manager import IncidentManager
from models import (
    Candidate,
    Commit,
    DetectorFlags,
    IncidentEventKind,
    IncidentStatus,
    Repository,
    Route,
    SecretType,
    Severity,
)
from storage import SqliteStorage


NOW = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)
FP = b"\x02" * 32


class IncidentManagerTests(unittest.IsolatedAsyncioTestCase):
    """Проверить дедупликацию, reopen, повышение критичности и rollback."""

    async def asyncSetUp(self) -> None:
        """Подготовить SQLite в памяти и репозиторий с тремя коммитами."""
        self.storage = SqliteStorage(":memory:")
        await self.storage.initialize()
        self.manager = IncidentManager()
        self.repository = await self.storage.save_repository(
            Repository(location="repo", name="repo", created_at=NOW)
        )
        self.commits = [
            await self.storage.save_commit(
                Commit(repository_id=self.repository.id, sha=bytes([index]) * 20, committed_at=NOW)
            )
            for index in range(1, 4)
        ]

    async def asyncTearDown(self) -> None:
        """Закрыть соединение после проверки."""
        await self.storage.close()

    async def _candidate(self, index: int, **overrides: object) -> Candidate:
        """Сохранить безопасный кандидат с общим fingerprint."""
        data = dict(
            repository_id=self.repository.id,
            commit_id=self.commits[index].id,
            file_path=f"config/{index}.py",
            line_number=1,
            key_name="AWS_ACCESS_KEY",
            secret_type=SecretType.AWS_ACCESS_KEY,
            value_masked="AKIA...MPLE",
            value_fingerprint=FP,
            entropy=3.8,
            detector_flags=DetectorFlags.REGEX | DetectorFlags.CONTEXT,
            source_line_masked="AWS_ACCESS_KEY=AKIA...MPLE",
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
        data.update(overrides)
        return await self.storage.save_candidate(Candidate(**data))

    async def test_three_commits_one_incident_and_repeat_is_noop(self) -> None:
        """Три обнаружения дают один инцидент, повтор не меняет журнал."""
        candidates = [await self._candidate(index) for index in range(3)]
        for candidate in candidates:
            incident = await self.storage.run_in_transaction(
                lambda session, candidate=candidate: self.manager.process(session, candidate, now=NOW)
            )
        self.assertEqual(incident.severity, Severity.HIGH)
        graph = await self.storage.read(
            lambda session: (
                session.count("incidents"),
                session.list_occurrences(incident.id),
                session.list_incident_events(incident.id),
            )
        )
        self.assertEqual(graph[0], 1)
        self.assertEqual({item.candidate_id for item in graph[1]}, {item.id for item in candidates})
        self.assertEqual([event.kind for event in graph[2]], [
            IncidentEventKind.OPENED,
            IncidentEventKind.OCCURRENCE_ADDED,
            IncidentEventKind.OCCURRENCE_ADDED,
        ])
        version = incident.version
        repeated = await self.storage.run_in_transaction(
            lambda session: self.manager.process(session, candidates[2], now=NOW)
        )
        self.assertEqual(repeated.version, version)
        self.assertEqual(await self.storage.read(lambda session: session.count("occurrences")), 3)
        self.assertEqual(await self.storage.read(lambda session: session.count("incident_events")), 3)

    async def test_reopen_and_head_bump(self) -> None:
        """Закрытый инцидент открывается вновь, HEAD повышает severity."""
        first = await self._candidate(0)
        incident = await self.storage.run_in_transaction(
            lambda session: self.manager.process(session, first, now=NOW)
        )

        def close(session):
            current = session.get_incident(incident.id)
            previous = current.version
            event = current.set_status(IncidentStatus.REMEDIATED, "user", None, now=NOW)
            session.save_incident_event(event)
            session.save_incident(current, expected_version=previous)

        await self.storage.run_in_transaction(close)
        second = await self._candidate(1, score_total=0.95)
        reopened = await self.storage.run_in_transaction(
            lambda session: self.manager.process(session, second, still_in_head=True, now=NOW)
        )
        self.assertEqual(reopened.status, IncidentStatus.OPEN)
        self.assertIsNone(reopened.resolved_at)
        self.assertEqual(reopened.severity, Severity.CRITICAL)
        self.assertTrue(reopened.still_in_head)
        kinds = await self.storage.read(
            lambda session: [event.kind for event in session.list_incident_events(incident.id)]
        )
        self.assertIn(IncidentEventKind.REOPENED, kinds)
        self.assertIn(IncidentEventKind.SEVERITY_RAISED, kinds)

    async def test_policy_filters_and_transaction_rollback(self) -> None:
        """Недоверенный формат не открывает инцидент, ошибка откатывает граф."""
        generic = await self._candidate(0, secret_type=SecretType.GENERIC_PASSWORD)
        result = await self.storage.run_in_transaction(
            lambda session: self.manager.process(session, generic, now=NOW)
        )
        self.assertIsNone(result)
        test_candidate = await self._candidate(0, file_path="tests/example.py", is_test_context=True)
        self.assertIsNone(await self.storage.run_in_transaction(
            lambda session: self.manager.process(session, test_candidate, now=NOW)
        ))
        trusted = await self._candidate(1)

        def fail(session):
            self.manager.process(session, trusted, now=NOW)
            raise RuntimeError("rollback")

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            await self.storage.run_in_transaction(fail)
        counts = await self.storage.read(
            lambda session: tuple(session.count(name) for name in ("incidents", "occurrences", "incident_events"))
        )
        self.assertEqual(counts, (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
