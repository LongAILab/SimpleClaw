"""Repository protocols for shared runtime state."""

from __future__ import annotations

from typing import Any, Protocol

from simpleclaw.cron.types import CronJob, CronSchedule
from simpleclaw.runtime.task_protocol import TaskEnvelope
from simpleclaw.session.manager import Session
from simpleclaw.tenant.state import TenantState


class LeaseStore(Protocol):
    def acquire(self, scope: str, key: str, *, ttl_s: int, owner: str | None = None) -> bool: ...
    def renew(self, scope: str, key: str, *, ttl_s: int, owner: str | None = None) -> bool: ...
    def release(self, scope: str, key: str, *, owner: str | None = None) -> None: ...
    def is_active(self, scope: str, key: str) -> bool: ...


class TenantStateStore(Protocol):
    def get(self, tenant_key: str | None) -> TenantState | None: ...
    def get_or_create(self, tenant_key: str | None) -> TenantState: ...
    def save(self, state: TenantState) -> TenantState: ...
    def touch_interaction(
        self,
        *,
        tenant_key: str | None,
        session_key: str,
        channel: str,
        chat_id: str,
        activity_at_ms: int | None = None,
    ) -> TenantState: ...
    def configure_heartbeat(
        self,
        tenant_key: str | None,
        *,
        enabled: bool | None = None,
        interval_s: int | None = None,
        next_run_at_ms: int | None = None,
    ) -> TenantState: ...
    def mark_heartbeat_result(
        self,
        tenant_key: str | None,
        *,
        status: str,
        next_run_at_ms: int,
        error: str | None = None,
        ran_at_ms: int | None = None,
    ) -> TenantState: ...
    def list_states(self) -> list[TenantState]: ...
    def list_due_heartbeat_tenants(
        self,
        *,
        now_ms: int | None = None,
        tenant_filter: str | None = None,
    ) -> list[TenantState]: ...


class CronJobStore(Protocol):
    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]: ...
    def get_job(self, job_id: str) -> CronJob | None: ...
    def list_due_jobs(self, current_ms: int | None = None) -> list[CronJob]: ...
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
    ) -> CronJob: ...
    def remove_job(self, job_id: str) -> bool: ...
    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None: ...
    def finalize_job_run(
        self,
        job_id: str,
        *,
        started_ms: int,
        status: str,
        error: str | None,
    ) -> CronJob | None: ...
    def recompute_next_runs(self) -> None: ...
    def next_wake_at_ms(self) -> int | None: ...


class SessionStore(Protocol):
    def load(self, tenant_key: str, session_key: str) -> Session | None: ...
    def save(self, tenant_key: str, session: Session) -> None: ...
    def list_sessions(self, tenant_key: str | None = None) -> list[dict[str, Any]]: ...


class TaskStateStore(Protocol):
    def enqueue(self, task: TaskEnvelope, *, queue_message_id: str | None = None) -> None: ...
    def mark_claimed(self, task: TaskEnvelope, *, consumer_name: str, attempt: int) -> None: ...
    def mark_succeeded(self, task: TaskEnvelope) -> None: ...
    def mark_failed(self, task: TaskEnvelope, *, error: str, dead_letter: bool) -> None: ...

