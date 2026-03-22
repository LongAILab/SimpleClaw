"""Cron service facade backed by repository + scheduler."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Coroutine

from nanobot.cron.repository import CronRepository
from nanobot.cron.scheduler import CronScheduler
from nanobot.cron.types import CronJob, CronSchedule
from nanobot.storage.interfaces import CronJobStore, LeaseStore


class CronService:
    """Facade preserving the old CronService API."""

    def __init__(
        self,
        store_path: Path,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
        *,
        repository: CronJobStore | None = None,
        lease_repo: LeaseStore | None = None,
        enabled: bool = True,
        poll_interval_s: int = 1,
        claim_ttl_s: int = 10 * 60,
        max_concurrency: int = 4,
    ) -> None:
        self.store_path = store_path
        self.on_job = on_job
        self.enabled = enabled
        self.repository = repository or CronRepository(store_path)
        self.lease_repo = lease_repo or LeaseRepository()
        self.scheduler = CronScheduler(
            self.repository,
            self.lease_repo,
            on_job=on_job,
            poll_interval_s=poll_interval_s,
            claim_ttl_s=claim_ttl_s,
            max_concurrency=max_concurrency,
        )

    async def start(self) -> None:
        """Start the cron scheduler."""
        self.scheduler.on_job = self.on_job
        if not self.enabled:
            return
        await self.scheduler.start()

    def stop(self) -> None:
        """Stop the cron scheduler."""
        self.scheduler.stop()

    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        return self.repository.list_jobs(include_disabled=include_disabled)

    def add_job(
        self,
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
        return self.repository.add_job(
            name=name,
            schedule=schedule,
            message=message,
            deliver=deliver,
            channel=channel,
            to=to,
            tenant_key=tenant_key,
            session_key=session_key,
            origin_session_key=origin_session_key,
            execution_policy=execution_policy,
            delete_after_run=delete_after_run,
        )

    def remove_job(self, job_id: str) -> bool:
        return self.repository.remove_job(job_id)

    def enable_job(self, job_id: str, enabled: bool = True) -> CronJob | None:
        return self.repository.enable_job(job_id, enabled=enabled)

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        if not self.enabled and not force:
            return False
        return await self.scheduler.run_job(job_id, force=force)

    def status(self) -> dict[str, int | bool | None]:
        status = self.scheduler.status()
        status["configured_enabled"] = self.enabled
        if not self.enabled:
            status["enabled"] = False
        return status
