"""MySQL-backed cron repository."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from nanobot.cron.repository import compute_next_run
from nanobot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule

from .mysql_database import MySQLDatabase
from .mysql_datetime import _dt_to_ms, _ms_to_dt, _now_ms


class MySQLCronRepository:
    """MySQL-backed cron repository compatible with the current API."""

    def __init__(self, db: MySQLDatabase) -> None:
        self._db = db

    @staticmethod
    def _job_to_row(job: CronJob) -> tuple[Any, ...]:
        return (
            job.id,
            job.payload.tenant_key,
            job.name,
            1 if job.enabled else 0,
            json.dumps(asdict(job.schedule), ensure_ascii=False),
            json.dumps(asdict(job.payload), ensure_ascii=False),
            json.dumps(asdict(job.state), ensure_ascii=False),
            _ms_to_dt(job.created_at_ms),
            _ms_to_dt(job.updated_at_ms),
            1 if job.delete_after_run else 0,
        )

    @staticmethod
    def _row_to_job(row: dict[str, Any]) -> CronJob:
        schedule = json.loads(row["schedule_json"])
        payload = json.loads(row["payload_json"])
        state = json.loads(row["state_json"])
        return CronJob(
            id=str(row["job_id"]),
            name=str(row["name"]),
            enabled=bool(row["enabled"]),
            schedule=CronSchedule(
                kind=schedule["kind"],
                at_ms=schedule.get("at_ms"),
                every_ms=schedule.get("every_ms"),
                expr=schedule.get("expr"),
                tz=schedule.get("tz"),
            ),
            payload=CronPayload(
                kind=payload.get("kind", "agent_turn"),
                message=payload.get("message", ""),
                deliver=payload.get("deliver", False),
                channel=payload.get("channel"),
                to=payload.get("to"),
                tenant_key=payload.get("tenant_key"),
                session_key=payload.get("session_key"),
                origin_session_key=payload.get("origin_session_key"),
                execution_policy=payload.get("execution_policy"),
            ),
            state=CronJobState(
                next_run_at_ms=state.get("next_run_at_ms"),
                last_run_at_ms=state.get("last_run_at_ms"),
                last_status=state.get("last_status"),
                last_error=state.get("last_error"),
            ),
            created_at_ms=int(_dt_to_ms(row.get("created_at") or row.get("created_at_ms")) or 0),
            updated_at_ms=int(_dt_to_ms(row.get("updated_at") or row.get("updated_at_ms")) or 0),
            delete_after_run=bool(row.get("delete_after_run")),
        )

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        sql = "SELECT * FROM nb_cron_jobs"
        params: tuple[Any, ...] = ()
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY updated_at DESC"
        return [self._row_to_job(row) for row in self._db.fetch_all(sql, params)]

    def get_job(self, job_id: str) -> CronJob | None:
        row = self._db.fetch_one("SELECT * FROM nb_cron_jobs WHERE job_id=%s", (job_id,))
        return self._row_to_job(row) if row else None

    def list_due_jobs(self, current_ms: int | None = None) -> list[CronJob]:
        cutoff = current_ms or _now_ms()
        rows = self._db.fetch_all(
            """
            SELECT * FROM nb_cron_jobs
            WHERE enabled=1
              AND JSON_EXTRACT(state_json, '$.next_run_at_ms') IS NOT NULL
              AND CAST(JSON_UNQUOTE(JSON_EXTRACT(state_json, '$.next_run_at_ms')) AS SIGNED) <= %s
            ORDER BY updated_at DESC
            """,
            (cutoff,),
        )
        return [self._row_to_job(row) for row in rows]

    def add_job(
        self,
        *,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: str | None = None,
        to: str | None = None,
        tenant_key: str | None = None,
        session_key: str | None = None,
        origin_session_key: str | None = None,
        execution_policy: str | None = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        from nanobot.cron.repository import validate_schedule_for_add
        import uuid

        validate_schedule_for_add(schedule)
        current_ms = _now_ms()
        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
                tenant_key=tenant_key,
                session_key=session_key,
                origin_session_key=origin_session_key or session_key,
                execution_policy=execution_policy,
            ),
            state=CronJobState(next_run_at_ms=compute_next_run(schedule, current_ms)),
            created_at_ms=current_ms,
            updated_at_ms=current_ms,
            delete_after_run=delete_after_run,
        )
        self.save_job(job)
        return job

    def save_job(self, job: CronJob) -> None:
        self._db.execute(
            """
            INSERT INTO nb_cron_jobs (
                job_id, tenant_key, name, enabled, schedule_json, payload_json, state_json,
                created_at, updated_at, delete_after_run
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                tenant_key=VALUES(tenant_key),
                name=VALUES(name),
                enabled=VALUES(enabled),
                schedule_json=VALUES(schedule_json),
                payload_json=VALUES(payload_json),
                state_json=VALUES(state_json),
                created_at=VALUES(created_at),
                updated_at=VALUES(updated_at),
                delete_after_run=VALUES(delete_after_run)
            """,
            self._job_to_row(job),
        )

    def remove_job(self, job_id: str) -> bool:
        return bool(self._db.execute("DELETE FROM nb_cron_jobs WHERE job_id=%s", (job_id,)))

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        job.enabled = enabled
        job.updated_at_ms = _now_ms()
        self.save_job(job)
        return job

    def finalize_job_run(
        self,
        job_id: str,
        *,
        started_ms: int,
        status: str,
        error: str | None,
    ) -> CronJob | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        job.state.last_run_at_ms = started_ms
        job.state.last_status = status
        job.state.last_error = error
        if job.delete_after_run and status == "ok":
            self.remove_job(job_id)
            return None
        job.state.next_run_at_ms = compute_next_run(job.schedule, started_ms)
        job.updated_at_ms = _now_ms()
        self.save_job(job)
        return job

    def recompute_next_runs(self) -> None:
        current_ms = _now_ms()
        for job in self.list_jobs(include_disabled=True):
            if not job.enabled:
                continue
            job.state.next_run_at_ms = compute_next_run(job.schedule, current_ms)
            job.updated_at_ms = current_ms
            self.save_job(job)

    def next_wake_at_ms(self) -> int | None:
        rows = self._db.fetch_all(
            """
            SELECT CAST(JSON_UNQUOTE(JSON_EXTRACT(state_json, '$.next_run_at_ms')) AS SIGNED) AS next_run
            FROM nb_cron_jobs
            WHERE enabled=1
              AND JSON_EXTRACT(state_json, '$.next_run_at_ms') IS NOT NULL
            ORDER BY next_run ASC
            LIMIT 1
            """
        )
        if not rows:
            return None
        return rows[0].get("next_run")
