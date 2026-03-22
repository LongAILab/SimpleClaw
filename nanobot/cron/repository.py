"""File-backed cron repository."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from nanobot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule, CronStore


def now_ms() -> int:
    return int(time.time() * 1000)


def compute_next_run(schedule: CronSchedule, base_ms: int) -> int | None:
    """Compute next run time in ms."""
    if schedule.kind == "at":
        return schedule.at_ms if schedule.at_ms and schedule.at_ms > base_ms else None

    if schedule.kind == "every":
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return base_ms + schedule.every_ms

    if schedule.kind == "cron" and schedule.expr:
        try:
            from zoneinfo import ZoneInfo

            from croniter import croniter

            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            cron = croniter(schedule.expr, datetime.fromtimestamp(base_ms / 1000, tz=tz))
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def validate_schedule_for_add(schedule: CronSchedule) -> None:
    """Validate schedule fields that would otherwise create non-runnable jobs."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")
    if schedule.kind == "cron" and schedule.tz:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(schedule.tz)
        except Exception:
            raise ValueError(f"unknown timezone '{schedule.tz}'") from None


@contextmanager
def _locked_file(path: Path) -> Iterator[object]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        yield f
        f.flush()
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class CronRepository:
    """Persistent cron job repository."""

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path

    def _deserialize(self, data: dict) -> CronStore:
        jobs = []
        for item in data.get("jobs", []):
            jobs.append(CronJob(
                id=item["id"],
                name=item["name"],
                enabled=item.get("enabled", True),
                schedule=CronSchedule(
                    kind=item["schedule"]["kind"],
                    at_ms=item["schedule"].get("atMs"),
                    every_ms=item["schedule"].get("everyMs"),
                    expr=item["schedule"].get("expr"),
                    tz=item["schedule"].get("tz"),
                ),
                payload=CronPayload(
                    kind=item["payload"].get("kind", "agent_turn"),
                    message=item["payload"].get("message", ""),
                    deliver=item["payload"].get("deliver", False),
                    channel=item["payload"].get("channel"),
                    to=item["payload"].get("to"),
                    tenant_key=item["payload"].get("tenantKey"),
                    session_key=item["payload"].get("sessionKey"),
                    origin_session_key=item["payload"].get("originSessionKey") or item["payload"].get("sessionKey"),
                    execution_policy=item["payload"].get("executionPolicy"),
                ),
                state=CronJobState(
                    next_run_at_ms=item.get("state", {}).get("nextRunAtMs"),
                    last_run_at_ms=item.get("state", {}).get("lastRunAtMs"),
                    last_status=item.get("state", {}).get("lastStatus"),
                    last_error=item.get("state", {}).get("lastError"),
                ),
                created_at_ms=item.get("createdAtMs", 0),
                updated_at_ms=item.get("updatedAtMs", 0),
                delete_after_run=item.get("deleteAfterRun", False),
            ))
        return CronStore(version=data.get("version", 1), jobs=jobs)

    def _serialize(self, store: CronStore) -> dict:
        return {
            "version": store.version,
            "jobs": [
                {
                    "id": job.id,
                    "name": job.name,
                    "enabled": job.enabled,
                    "schedule": {
                        "kind": job.schedule.kind,
                        "atMs": job.schedule.at_ms,
                        "everyMs": job.schedule.every_ms,
                        "expr": job.schedule.expr,
                        "tz": job.schedule.tz,
                    },
                    "payload": {
                        "kind": job.payload.kind,
                        "message": job.payload.message,
                        "deliver": job.payload.deliver,
                        "channel": job.payload.channel,
                        "to": job.payload.to,
                        "tenantKey": job.payload.tenant_key,
                        "sessionKey": job.payload.session_key,
                        "originSessionKey": job.payload.origin_session_key,
                        "executionPolicy": job.payload.execution_policy,
                    },
                    "state": {
                        "nextRunAtMs": job.state.next_run_at_ms,
                        "lastRunAtMs": job.state.last_run_at_ms,
                        "lastStatus": job.state.last_status,
                        "lastError": job.state.last_error,
                    },
                    "createdAtMs": job.created_at_ms,
                    "updatedAtMs": job.updated_at_ms,
                    "deleteAfterRun": job.delete_after_run,
                }
                for job in store.jobs
            ],
        }

    def load_store(self) -> CronStore:
        if not self.store_path.exists():
            return CronStore()
        with _locked_file(self.store_path) as f:
            raw = f.read().strip()
            if not raw:
                return CronStore()
            return self._deserialize(json.loads(raw))

    def save_store(self, store: CronStore) -> None:
        payload = json.dumps(self._serialize(store), ensure_ascii=False, indent=2)
        with _locked_file(self.store_path) as f:
            f.seek(0)
            f.truncate()
            f.write(payload)

    def recompute_next_runs(self) -> None:
        store = self.load_store()
        current_ms = now_ms()
        for job in store.jobs:
            if job.enabled:
                job.state.next_run_at_ms = compute_next_run(job.schedule, current_ms)
        self.save_store(store)

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        store = self.load_store()
        jobs = store.jobs if include_disabled else [job for job in store.jobs if job.enabled]
        return sorted(jobs, key=lambda job: job.state.next_run_at_ms or float("inf"))

    def get_job(self, job_id: str) -> CronJob | None:
        store = self.load_store()
        for job in store.jobs:
            if job.id == job_id:
                return job
        return None

    def list_due_jobs(self, current_ms: int | None = None) -> list[CronJob]:
        cutoff = current_ms or now_ms()
        return [
            job for job in self.list_jobs(include_disabled=False)
            if job.state.next_run_at_ms and job.state.next_run_at_ms <= cutoff
        ]

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
        validate_schedule_for_add(schedule)
        store = self.load_store()
        current_ms = now_ms()
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
        store.jobs.append(job)
        self.save_store(store)
        return job

    def remove_job(self, job_id: str) -> bool:
        store = self.load_store()
        before = len(store.jobs)
        store.jobs = [job for job in store.jobs if job.id != job_id]
        if len(store.jobs) == before:
            return False
        self.save_store(store)
        return True

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        store = self.load_store()
        for job in store.jobs:
            if job.id != job_id:
                continue
            job.enabled = enabled
            job.updated_at_ms = now_ms()
            job.state.next_run_at_ms = compute_next_run(job.schedule, now_ms()) if enabled else None
            self.save_store(store)
            return job
        return None

    def finalize_job_run(
        self,
        job_id: str,
        *,
        started_ms: int,
        status: str,
        error: str | None = None,
    ) -> CronJob | None:
        store = self.load_store()
        for job in store.jobs:
            if job.id != job_id:
                continue
            job.state.last_status = status
            job.state.last_error = error
            job.state.last_run_at_ms = started_ms
            job.updated_at_ms = now_ms()
            if job.schedule.kind == "at":
                if job.delete_after_run:
                    store.jobs = [item for item in store.jobs if item.id != job_id]
                    self.save_store(store)
                    return None
                job.enabled = False
                job.state.next_run_at_ms = None
            else:
                job.state.next_run_at_ms = compute_next_run(job.schedule, now_ms())
            self.save_store(store)
            return job
        return None

    def next_wake_at_ms(self) -> int | None:
        times = [
            job.state.next_run_at_ms
            for job in self.list_jobs(include_disabled=False)
            if job.state.next_run_at_ms is not None
        ]
        return min(times) if times else None
