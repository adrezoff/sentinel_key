"""Синхронная сессия одной SQLite-транзакции и mapper'ы доменных моделей."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection
from datetime import datetime, timezone
from typing import Any

from models import (
    Candidate,
    Commit,
    CommitParent,
    CommitStatus,
    DetectorFlags,
    ErrorCode,
    Incident,
    IncidentEvent,
    IncidentEventKind,
    IncidentStatus,
    NotifyChannel,
    Occurrence,
    Outcome,
    OutboxMessage,
    OutboxStatus,
    Repository,
    Route,
    SecretType,
    Severity,
    Task,
    TaskKind,
    TaskResult,
    TaskStatus,
    dump_output,
    dump_payload,
    parse_output,
    parse_payload,
)

from .exceptions import ConcurrentUpdateError


def _to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _from_epoch_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


class StorageSession:
    """Набор операций над моделями внутри одного SQLite-соединения.

    Экземпляр выдаётся только callback-функции ``run_in_transaction`` и не
    должен сохраняться или использоваться из другого потока.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def save_repository(self, repository: Repository) -> Repository:
        """Вставить репозиторий или обновить его данные по уникальному location."""
        self._connection.execute(
            """
            INSERT INTO repositories(
                location, name, default_ref, created_at, last_scan_at,
                last_scan_task_id, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(location) DO UPDATE SET
                name = excluded.name,
                default_ref = excluded.default_ref
            """,
            (
                repository.location,
                repository.name,
                repository.default_ref,
                _to_epoch_ms(repository.created_at),
                _to_epoch_ms(repository.last_scan_at),
                repository.last_scan_task_id,
                repository.version,
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM repositories WHERE location = ?", (repository.location,)
        ).fetchone()
        assert row is not None
        stored = self._repository_from_row(row)
        repository.id = stored.id
        return stored

    def get_repository(self, repository_id: int) -> Repository | None:
        """Загрузить репозиторий по первичному ключу."""
        row = self._connection.execute(
            "SELECT * FROM repositories WHERE id = ?", (repository_id,)
        ).fetchone()
        return None if row is None else self._repository_from_row(row)

    def get_repository_by_location(self, location: str) -> Repository | None:
        """Найти зарегистрированный репозиторий по нормализованному пути."""
        row = self._connection.execute(
            "SELECT * FROM repositories WHERE location = ?", (location,)
        ).fetchone()
        return None if row is None else self._repository_from_row(row)

    def update_repository(
        self, repository: Repository, *, expected_version: int
    ) -> Repository:
        """Обновить репозиторий с optimistic lock и увеличить version."""
        if repository.id is None:
            raise ValueError("repository.id is required for update")
        new_version = expected_version + 1
        cursor = self._connection.execute(
            """
            UPDATE repositories SET
                location = ?, name = ?, default_ref = ?, last_scan_at = ?,
                last_scan_task_id = ?, version = ?
            WHERE id = ? AND version = ?
            """,
            (
                repository.location,
                repository.name,
                repository.default_ref,
                _to_epoch_ms(repository.last_scan_at),
                repository.last_scan_task_id,
                new_version,
                repository.id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ConcurrentUpdateError("repository version conflict")
        repository.version = new_version
        return repository

    def save_commit(self, commit: Commit) -> Commit:
        """Идемпотентно сохранить коммит по ``(repository_id, sha)``."""
        self._connection.execute(
            """
            INSERT INTO commits(
                repository_id, sha, status, is_merge, committed_at,
                last_task_id, scan_run_id, processed_at, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repository_id, sha) DO NOTHING
            """,
            (
                commit.repository_id,
                commit.sha,
                int(commit.status),
                int(commit.is_merge),
                _to_epoch_ms(commit.committed_at),
                commit.last_task_id,
                commit.scan_run_id,
                _to_epoch_ms(commit.processed_at),
                None if commit.error_code is None else int(commit.error_code),
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM commits WHERE repository_id = ? AND sha = ?",
            (commit.repository_id, commit.sha),
        ).fetchone()
        assert row is not None
        stored = self._commit_from_row(row)
        commit.id = stored.id
        return stored

    def update_commit(self, commit: Commit) -> None:
        """Сохранить статус и служебные поля существующего коммита."""
        if commit.id is None:
            raise ValueError("commit.id is required for update")
        cursor = self._connection.execute(
            """
            UPDATE commits SET
                status = ?, is_merge = ?, committed_at = ?, last_task_id = ?,
                scan_run_id = ?, processed_at = ?, error_code = ?
            WHERE id = ?
            """,
            (
                int(commit.status),
                int(commit.is_merge),
                _to_epoch_ms(commit.committed_at),
                commit.last_task_id,
                commit.scan_run_id,
                _to_epoch_ms(commit.processed_at),
                None if commit.error_code is None else int(commit.error_code),
                commit.id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"commit not found: {commit.id}")

    def get_commit(self, commit_id: int) -> Commit | None:
        """Загрузить коммит по первичному ключу."""
        row = self._connection.execute(
            "SELECT * FROM commits WHERE id = ?", (commit_id,)
        ).fetchone()
        return None if row is None else self._commit_from_row(row)

    def has_processed_commits(self, repository_id: int) -> bool:
        """Проверить, был ли в репозитории хотя бы один обработанный SHA."""
        row = self._connection.execute(
            "SELECT 1 FROM commits WHERE repository_id = ? AND status = ? LIMIT 1",
            (repository_id, int(CommitStatus.PROCESSED)),
        ).fetchone()
        return row is not None

    def save_commit_parent(self, parent: CommitParent) -> None:
        """Идемпотентно сохранить ребро графа родителей коммита."""
        self._connection.execute(
            """
            INSERT INTO commit_parents(commit_id, position, parent_sha)
            VALUES (?, ?, ?)
            ON CONFLICT(commit_id, position) DO UPDATE SET parent_sha = excluded.parent_sha
            """,
            (parent.commit_id, parent.position, parent.parent_sha),
        )

    def list_commit_parents(self, commit_id: int) -> list[CommitParent]:
        """Загрузить родителей коммита в порядке Git first-parent."""
        rows = self._connection.execute(
            """
            SELECT commit_id, position, parent_sha
            FROM commit_parents
            WHERE commit_id = ?
            ORDER BY position
            """,
            (commit_id,),
        ).fetchall()
        return [
            CommitParent(
                commit_id=row["commit_id"],
                position=row["position"],
                parent_sha=bytes(row["parent_sha"]),
            )
            for row in rows
        ]

    def save_task(self, task: Task) -> Task:
        """Идемпотентно вставить задачу и вернуть сохранённую запись."""
        self._connection.execute(
            """
            INSERT INTO tasks(
                kind, repository_id, status, payload, idempotency_key,
                root_task_id, parent_task_id, priority, attempts, max_attempts,
                not_before, lease_owner, lease_expires_at, error_code,
                error_message, error_retryable, created_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            self._task_values(task),
        )
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?", (task.idempotency_key,)
        ).fetchone()
        assert row is not None
        stored = self._task_from_row(row)
        task.id = stored.id
        return stored

    def update_task(self, task: Task) -> None:
        """Сохранить задачу после перехода, выполненного методом доменной модели."""
        if task.id is None:
            raise ValueError("task.id is required for update")
        cursor = self._connection.execute(
            """
            UPDATE tasks SET
                kind = ?, repository_id = ?, status = ?, payload = ?,
                idempotency_key = ?, root_task_id = ?, parent_task_id = ?,
                priority = ?, attempts = ?, max_attempts = ?, not_before = ?,
                lease_owner = ?, lease_expires_at = ?, error_code = ?,
                error_message = ?, error_retryable = ?, created_at = ?,
                started_at = ?, finished_at = ?
            WHERE id = ?
            """,
            (*self._task_values(task), task.id),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"task not found: {task.id}")

    def get_task(self, task_id: int) -> Task | None:
        """Загрузить задачу с типизированным payload."""
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return None if row is None else self._task_from_row(row)

    def get_active_scan(self, repository_id: int) -> Task | None:
        """Найти единственный нетерминальный запуск репозитория."""
        row = self._connection.execute(
            """
            SELECT * FROM tasks
            WHERE repository_id = ? AND kind = ? AND status IN (?, ?, ?)
            LIMIT 1
            """,
            (
                repository_id,
                int(TaskKind.REPOSITORY_SCAN),
                int(TaskStatus.QUEUED),
                int(TaskStatus.RUNNING),
                int(TaskStatus.WAITING_CHILDREN),
            ),
        ).fetchone()
        return None if row is None else self._task_from_row(row)

    def find_next_queued_task(
        self,
        kinds: Collection[TaskKind],
        now: datetime,
    ) -> Task | None:
        """Найти следующую готовую задачу внутри транзакции claim."""
        kind_values = tuple(int(kind) for kind in kinds)
        if not kind_values:
            return None
        placeholders = ",".join("?" for _ in kind_values)
        row = self._connection.execute(
            f"""
            SELECT * FROM tasks
            WHERE status = ?
              AND kind IN ({placeholders})
              AND (not_before IS NULL OR not_before <= ?)
            ORDER BY priority, id
            LIMIT 1
            """,
            (int(TaskStatus.QUEUED), *kind_values, _to_epoch_ms(now)),
        ).fetchone()
        return None if row is None else self._task_from_row(row)

    def list_expired_tasks(self, now: datetime) -> list[Task]:
        """Загрузить RUNNING-задачи с истёкшей арендой в порядке id."""
        rows = self._connection.execute(
            """
            SELECT * FROM tasks
            WHERE status = ? AND lease_expires_at <= ?
            ORDER BY id
            """,
            (int(TaskStatus.RUNNING), _to_epoch_ms(now)),
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def list_active_run_tasks(self, run_id: int) -> list[Task]:
        """Загрузить нетерминальные корневую и дочерние задачи запуска."""
        rows = self._connection.execute(
            """
            SELECT * FROM tasks
            WHERE (id = ? OR root_task_id = ?)
              AND status IN (?, ?, ?)
            ORDER BY id
            """,
            (
                run_id,
                run_id,
                int(TaskStatus.QUEUED),
                int(TaskStatus.RUNNING),
                int(TaskStatus.WAITING_CHILDREN),
            ),
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def list_run_tasks(self, run_id: int) -> list[Task]:
        """Загрузить все дочерние задачи запуска для finalizer."""
        rows = self._connection.execute(
            "SELECT * FROM tasks WHERE root_task_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def list_waiting_scans(self) -> list[Task]:
        """Загрузить корневые задачи, ожидающие завершения детей."""
        rows = self._connection.execute(
            "SELECT * FROM tasks WHERE kind = ? AND status = ? ORDER BY id",
            (int(TaskKind.REPOSITORY_SCAN), int(TaskStatus.WAITING_CHILDREN)),
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def save_task_result(self, result: TaskResult) -> None:
        """Сохранить единственный результат задачи с типизированным output в JSON."""
        self._connection.execute(
            """
            INSERT INTO task_results(
                task_id, outcome, output, error_code, error_message,
                error_retryable, metrics, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                outcome = excluded.outcome,
                output = excluded.output,
                error_code = excluded.error_code,
                error_message = excluded.error_message,
                error_retryable = excluded.error_retryable,
                metrics = excluded.metrics,
                created_at = excluded.created_at
            """,
            (
                result.task_id,
                int(result.outcome),
                None if result.output is None else _json_dump(dump_output(result.output)),
                None if result.error_code is None else int(result.error_code),
                result.error_message,
                None if result.error_retryable is None else int(result.error_retryable),
                _json_dump(result.metrics),
                _to_epoch_ms(result.created_at),
            ),
        )

    def get_task_result(self, task_id: int) -> TaskResult | None:
        """Загрузить результат, выбрав тип output по виду исходной задачи."""
        row = self._connection.execute(
            """
            SELECT r.*, t.kind AS task_kind
            FROM task_results AS r
            JOIN tasks AS t ON t.id = r.task_id
            WHERE r.task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        kind = TaskKind(row["task_kind"])
        return TaskResult(
            task_id=row["task_id"],
            outcome=Outcome(row["outcome"]),
            output=parse_output(kind, _json_load(row["output"])),
            error_code=None if row["error_code"] is None else ErrorCode(row["error_code"]),
            error_message=row["error_message"],
            error_retryable=_bool(row["error_retryable"]),
            metrics=_json_load(row["metrics"]),
            created_at=_from_epoch_ms(row["created_at"]),
        )

    def save_candidate(self, candidate: Candidate) -> Candidate:
        """Идемпотентно сохранить безопасную модель кандидата без сырого секрета."""
        if candidate.route is Route.IGNORE:
            raise ValueError("IGNORE candidates must not be persisted")
        self._connection.execute(
            """
            INSERT INTO candidates(
                repository_id, commit_id, file_path, line_number, key_name,
                secret_type, value_masked, value_fingerprint, entropy,
                regex_rules, detector_flags, source_line_masked, context_masked,
                is_test_context, score_regex, score_entropy, score_context,
                score_token_type, score_total, route, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(commit_id, file_path, line_number, value_fingerprint) DO NOTHING
            """,
            (
                candidate.repository_id,
                candidate.commit_id,
                candidate.file_path,
                candidate.line_number,
                candidate.key_name,
                int(candidate.secret_type),
                candidate.value_masked,
                candidate.value_fingerprint,
                candidate.entropy,
                _json_dump(candidate.regex_rules),
                int(candidate.detector_flags),
                candidate.source_line_masked,
                candidate.context_masked,
                int(candidate.is_test_context),
                candidate.score_regex,
                candidate.score_entropy,
                candidate.score_context,
                candidate.score_token_type,
                candidate.score_total,
                int(candidate.route),
                _to_epoch_ms(candidate.created_at),
            ),
        )
        row = self._connection.execute(
            """
            SELECT * FROM candidates
            WHERE commit_id = ? AND file_path = ? AND line_number = ?
              AND value_fingerprint = ?
            """,
            (
                candidate.commit_id,
                candidate.file_path,
                candidate.line_number,
                candidate.value_fingerprint,
            ),
        ).fetchone()
        assert row is not None
        stored = self._candidate_from_row(row)
        candidate.id = stored.id
        return stored

    def get_candidate(self, candidate_id: int) -> Candidate | None:
        """Загрузить кандидата по первичному ключу."""
        row = self._connection.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        return None if row is None else self._candidate_from_row(row)

    def save_incident(
        self, incident: Incident, *, expected_version: int | None = None
    ) -> Incident:
        """Вставить инцидент либо сохранить изменённую модель с optimistic lock."""
        if incident.id is None:
            self._connection.execute(
                """
                INSERT INTO incidents(
                    repository_id, fingerprint, secret_type, severity, confidence,
                    detector_flags, evidence_masked, reason, recommendation, status,
                    still_in_head, assignee, first_commit_id, last_verdict_id,
                    created_at, updated_at, resolved_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repository_id, fingerprint) DO NOTHING
                """,
                self._incident_values(incident),
            )
            row = self._connection.execute(
                "SELECT * FROM incidents WHERE repository_id = ? AND fingerprint = ?",
                (incident.repository_id, incident.fingerprint),
            ).fetchone()
            assert row is not None
            stored = self._incident_from_row(row)
            incident.id = stored.id
            return stored

        params: list[Any] = [*self._incident_values(incident), incident.id]
        where = "id = ?"
        if expected_version is not None:
            where += " AND version = ?"
            params.append(expected_version)
        cursor = self._connection.execute(
            f"""
            UPDATE incidents SET
                repository_id = ?, fingerprint = ?, secret_type = ?, severity = ?,
                confidence = ?, detector_flags = ?, evidence_masked = ?, reason = ?,
                recommendation = ?, status = ?, still_in_head = ?, assignee = ?,
                first_commit_id = ?, last_verdict_id = ?, created_at = ?, updated_at = ?,
                resolved_at = ?, version = ?
            WHERE {where}
            """,
            params,
        )
        if cursor.rowcount != 1:
            raise ConcurrentUpdateError("incident version conflict")
        return incident

    def get_incident(self, incident_id: int) -> Incident | None:
        """Загрузить инцидент по первичному ключу."""
        row = self._connection.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        return None if row is None else self._incident_from_row(row)

    def get_incident_by_fingerprint(
        self, repository_id: int, fingerprint: bytes
    ) -> Incident | None:
        """Найти инцидент по естественному ключу дедупликации."""
        row = self._connection.execute(
            "SELECT * FROM incidents WHERE repository_id = ? AND fingerprint = ?",
            (repository_id, fingerprint),
        ).fetchone()
        return None if row is None else self._incident_from_row(row)

    def save_occurrence(self, occurrence: Occurrence) -> None:
        """Идемпотентно связать инцидент с кандидатом."""
        self._connection.execute(
            """
            INSERT INTO occurrences(incident_id, candidate_id, ai_task_id, seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(incident_id, candidate_id) DO NOTHING
            """,
            (
                occurrence.incident_id,
                occurrence.candidate_id,
                occurrence.ai_task_id,
                _to_epoch_ms(occurrence.seen_at),
            ),
        )

    def has_occurrence(self, incident_id: int, candidate_id: int) -> bool:
        """Проверить повторную обработку кандидата без загрузки всего списка."""
        row = self._connection.execute(
            "SELECT 1 FROM occurrences WHERE incident_id = ? AND candidate_id = ?",
            (incident_id, candidate_id),
        ).fetchone()
        return row is not None

    def list_occurrences(self, incident_id: int) -> list[Occurrence]:
        """Загрузить обнаружения инцидента в порядке времени и candidate id."""
        rows = self._connection.execute(
            """
            SELECT incident_id, candidate_id, ai_task_id, seen_at
            FROM occurrences
            WHERE incident_id = ?
            ORDER BY seen_at, candidate_id
            """,
            (incident_id,),
        ).fetchall()
        return [
            Occurrence(
                incident_id=row["incident_id"],
                candidate_id=row["candidate_id"],
                ai_task_id=row["ai_task_id"],
                seen_at=_from_epoch_ms(row["seen_at"]),
            )
            for row in rows
        ]

    def save_incident_event(self, event: IncidentEvent) -> IncidentEvent:
        """Добавить неизменяемую запись журнала инцидента."""
        cursor = self._connection.execute(
            """
            INSERT INTO incident_events(incident_id, kind, note, actor, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.incident_id,
                int(event.kind),
                event.note,
                event.actor,
                _to_epoch_ms(event.created_at),
            ),
        )
        event.id = cursor.lastrowid
        return event

    def list_incident_events(self, incident_id: int) -> list[IncidentEvent]:
        """Загрузить append-only журнал инцидента в порядке id."""
        rows = self._connection.execute(
            "SELECT * FROM incident_events WHERE incident_id = ? ORDER BY id",
            (incident_id,),
        ).fetchall()
        return [
            IncidentEvent(
                id=row["id"],
                incident_id=row["incident_id"],
                kind=IncidentEventKind(row["kind"]),
                note=row["note"],
                actor=row["actor"],
                created_at=_from_epoch_ms(row["created_at"]),
            )
            for row in rows
        ]

    def save_outbox_message(self, message: OutboxMessage) -> OutboxMessage:
        """Добавить сообщение outbox в той же транзакции, что и инцидент."""
        cursor = self._connection.execute(
            """
            INSERT INTO outbox_messages(
                incident_id, channel, payload, status, attempts,
                not_before, created_at, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.incident_id,
                int(message.channel),
                _json_dump(message.payload),
                int(message.status),
                message.attempts,
                _to_epoch_ms(message.not_before),
                _to_epoch_ms(message.created_at),
                _to_epoch_ms(message.sent_at),
            ),
        )
        message.id = cursor.lastrowid
        return message

    def update_outbox_message(self, message: OutboxMessage) -> None:
        """Сохранить попытки и итог доставки существующего сообщения outbox."""
        if message.id is None:
            raise ValueError("message.id is required for update")
        cursor = self._connection.execute(
            """
            UPDATE outbox_messages SET
                status = ?, attempts = ?, not_before = ?, sent_at = ?
            WHERE id = ?
            """,
            (
                int(message.status),
                message.attempts,
                _to_epoch_ms(message.not_before),
                _to_epoch_ms(message.sent_at),
                message.id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"outbox message not found: {message.id}")

    def get_outbox_message(self, message_id: int) -> OutboxMessage | None:
        """Загрузить сообщение outbox по первичному ключу."""
        row = self._connection.execute(
            "SELECT * FROM outbox_messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        return OutboxMessage(
            id=row["id"],
            incident_id=row["incident_id"],
            channel=NotifyChannel(row["channel"]),
            payload=_json_load(row["payload"]),
            status=OutboxStatus(row["status"]),
            attempts=row["attempts"],
            not_before=_from_epoch_ms(row["not_before"]),
            created_at=_from_epoch_ms(row["created_at"]),
            sent_at=_from_epoch_ms(row["sent_at"]),
        )

    def count(self, table: str) -> int:
        """Вернуть число строк разрешённой таблицы; служит диагностике и тестам."""
        allowed = {
            "repositories",
            "commits",
            "commit_parents",
            "tasks",
            "task_results",
            "candidates",
            "ai_verdicts",
            "incidents",
            "occurrences",
            "incident_events",
            "outbox_messages",
        }
        if table not in allowed:
            raise ValueError(f"unsupported table: {table}")
        row = self._connection.execute(f"SELECT COUNT(*) AS value FROM {table}").fetchone()
        return int(row["value"])

    @staticmethod
    def _repository_from_row(row: sqlite3.Row) -> Repository:
        return Repository(
            id=row["id"],
            location=row["location"],
            name=row["name"],
            default_ref=row["default_ref"],
            created_at=_from_epoch_ms(row["created_at"]),
            last_scan_at=_from_epoch_ms(row["last_scan_at"]),
            last_scan_task_id=row["last_scan_task_id"],
            version=row["version"],
        )

    @staticmethod
    def _commit_from_row(row: sqlite3.Row) -> Commit:
        return Commit(
            id=row["id"],
            repository_id=row["repository_id"],
            sha=bytes(row["sha"]),
            status=CommitStatus(row["status"]),
            is_merge=bool(row["is_merge"]),
            committed_at=_from_epoch_ms(row["committed_at"]),
            last_task_id=row["last_task_id"],
            scan_run_id=row["scan_run_id"],
            processed_at=_from_epoch_ms(row["processed_at"]),
            error_code=None if row["error_code"] is None else ErrorCode(row["error_code"]),
        )

    @staticmethod
    def _task_values(task: Task) -> tuple[Any, ...]:
        return (
            int(task.kind),
            task.repository_id,
            int(task.status),
            _json_dump(dump_payload(task.payload)),
            task.idempotency_key,
            task.root_task_id,
            task.parent_task_id,
            task.priority,
            task.attempts,
            task.max_attempts,
            _to_epoch_ms(task.not_before),
            task.lease_owner,
            _to_epoch_ms(task.lease_expires_at),
            None if task.error_code is None else int(task.error_code),
            task.error_message,
            None if task.error_retryable is None else int(task.error_retryable),
            _to_epoch_ms(task.created_at),
            _to_epoch_ms(task.started_at),
            _to_epoch_ms(task.finished_at),
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> Task:
        kind = TaskKind(row["kind"])
        return Task(
            id=row["id"],
            kind=kind,
            repository_id=row["repository_id"],
            status=TaskStatus(row["status"]),
            payload=parse_payload(kind, _json_load(row["payload"])),
            idempotency_key=bytes(row["idempotency_key"]),
            root_task_id=row["root_task_id"],
            parent_task_id=row["parent_task_id"],
            priority=row["priority"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            not_before=_from_epoch_ms(row["not_before"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=_from_epoch_ms(row["lease_expires_at"]),
            error_code=None if row["error_code"] is None else ErrorCode(row["error_code"]),
            error_message=row["error_message"],
            error_retryable=_bool(row["error_retryable"]),
            created_at=_from_epoch_ms(row["created_at"]),
            started_at=_from_epoch_ms(row["started_at"]),
            finished_at=_from_epoch_ms(row["finished_at"]),
        )

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> Candidate:
        return Candidate(
            id=row["id"],
            repository_id=row["repository_id"],
            commit_id=row["commit_id"],
            file_path=row["file_path"],
            line_number=row["line_number"],
            key_name=row["key_name"],
            secret_type=SecretType(row["secret_type"]),
            value_masked=row["value_masked"],
            value_fingerprint=bytes(row["value_fingerprint"]),
            entropy=row["entropy"],
            regex_rules=_json_load(row["regex_rules"]),
            detector_flags=DetectorFlags(row["detector_flags"]),
            source_line_masked=row["source_line_masked"],
            context_masked=row["context_masked"],
            is_test_context=bool(row["is_test_context"]),
            score_regex=row["score_regex"],
            score_entropy=row["score_entropy"],
            score_context=row["score_context"],
            score_token_type=row["score_token_type"],
            score_total=row["score_total"],
            route=Route(row["route"]),
            created_at=_from_epoch_ms(row["created_at"]),
        )

    @staticmethod
    def _incident_values(incident: Incident) -> tuple[Any, ...]:
        return (
            incident.repository_id,
            incident.fingerprint,
            int(incident.secret_type),
            int(incident.severity),
            incident.confidence,
            int(incident.detector_flags),
            incident.evidence_masked,
            incident.reason,
            incident.recommendation,
            int(incident.status),
            None if incident.still_in_head is None else int(incident.still_in_head),
            incident.assignee,
            incident.first_commit_id,
            incident.last_verdict_id,
            _to_epoch_ms(incident.created_at),
            _to_epoch_ms(incident.updated_at),
            _to_epoch_ms(incident.resolved_at),
            incident.version,
        )

    @staticmethod
    def _incident_from_row(row: sqlite3.Row) -> Incident:
        return Incident(
            id=row["id"],
            repository_id=row["repository_id"],
            fingerprint=bytes(row["fingerprint"]),
            secret_type=SecretType(row["secret_type"]),
            severity=Severity(row["severity"]),
            confidence=row["confidence"],
            detector_flags=DetectorFlags(row["detector_flags"]),
            evidence_masked=row["evidence_masked"],
            reason=row["reason"],
            recommendation=row["recommendation"],
            status=IncidentStatus(row["status"]),
            still_in_head=_bool(row["still_in_head"]),
            assignee=row["assignee"],
            first_commit_id=row["first_commit_id"],
            last_verdict_id=row["last_verdict_id"],
            created_at=_from_epoch_ms(row["created_at"]),
            updated_at=_from_epoch_ms(row["updated_at"]),
            resolved_at=_from_epoch_ms(row["resolved_at"]),
            version=row["version"],
        )
