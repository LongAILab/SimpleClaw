"""MySQL-backed runtime task repository."""

from __future__ import annotations

from simpleclaw.runtime.task_protocol import TaskEnvelope

from .mysql_database import MySQLDatabase
from .mysql_datetime import _ms_to_dt, _now_dt


class MySQLTaskRepository:
    """Task lifecycle store used by producers and workers."""

    def __init__(self, db: MySQLDatabase) -> None:
        self._db = db

    def enqueue(self, task: TaskEnvelope, *, queue_message_id: str | None = None) -> None:
        now_dt = _now_dt()
        self._db.execute(
            """
            INSERT INTO nb_runtime_tasks (
                task_id, task_type, stream_name, tenant_key, session_key, trace_id, service_role,
                status, attempt, max_attempts, payload_json, queue_message_id, last_error,
                claimed_by, created_at, updated_at, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                task_type=VALUES(task_type),
                stream_name=VALUES(stream_name),
                tenant_key=VALUES(tenant_key),
                session_key=VALUES(session_key),
                trace_id=VALUES(trace_id),
                service_role=VALUES(service_role),
                status=VALUES(status),
                attempt=VALUES(attempt),
                max_attempts=VALUES(max_attempts),
                payload_json=VALUES(payload_json),
                queue_message_id=VALUES(queue_message_id),
                last_error=VALUES(last_error),
                claimed_by=VALUES(claimed_by),
                updated_at=VALUES(updated_at),
                completed_at=VALUES(completed_at)
            """,
            (
                task.task_id,
                task.task_type,
                task.stream,
                task.tenant_key,
                task.session_key,
                task.trace_id,
                task.service_role,
                "queued",
                task.attempt,
                task.max_attempts,
                task.to_json(),
                queue_message_id,
                None,
                None,
                _ms_to_dt(task.created_at_ms),
                now_dt,
                None,
            ),
        )

    def mark_claimed(self, task: TaskEnvelope, *, consumer_name: str, attempt: int) -> None:
        self._db.execute(
            """
            UPDATE nb_runtime_tasks
            SET status=%s, attempt=%s, claimed_by=%s, updated_at=%s
            WHERE task_id=%s
            """,
            ("running", attempt, consumer_name, _now_dt(), task.task_id),
        )

    def mark_succeeded(self, task: TaskEnvelope) -> None:
        now_dt = _now_dt()
        self._db.execute(
            """
            UPDATE nb_runtime_tasks
            SET status=%s, updated_at=%s, completed_at=%s, last_error=NULL
            WHERE task_id=%s
            """,
            ("succeeded", now_dt, now_dt, task.task_id),
        )

    def mark_failed(self, task: TaskEnvelope, *, error: str, dead_letter: bool) -> None:
        now_dt = _now_dt()
        self._db.execute(
            """
            UPDATE nb_runtime_tasks
            SET status=%s, updated_at=%s, completed_at=%s, last_error=%s
            WHERE task_id=%s
            """,
            ("dead-letter" if dead_letter else "failed", now_dt, now_dt, error, task.task_id),
        )
