"""Cron job scheduler separated from persistence."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

from loguru import logger

from nanobot.cron.repository import now_ms
from nanobot.cron.types import CronJob
from nanobot.storage.interfaces import CronJobStore, LeaseStore


class CronScheduler:
    """Poll due cron jobs, claim them, and execute them once."""

    def __init__(
        self,
        repository: CronJobStore,
        lease_repo: LeaseStore,
        *,
        on_job: Callable[[CronJob], Coroutine[Any, Any, str | None]] | None = None,
        poll_interval_s: int = 1,
        claim_ttl_s: int = 10 * 60,
        max_concurrency: int = 4,
    ) -> None:
        self.repository = repository
        self.lease_repo = lease_repo
        self.on_job = on_job
        self.poll_interval_s = max(1, poll_interval_s)
        self.claim_ttl_s = max(30, claim_ttl_s)
        self.max_concurrency = max(1, max_concurrency)
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.repository.recompute_next_runs()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Cron scheduler started")

    def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.run_due_jobs()
                await asyncio.sleep(self.poll_interval_s)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Cron scheduler tick failed")

    async def run_due_jobs(self) -> None:
        due_jobs = self.repository.list_due_jobs(now_ms())
        if not due_jobs:
            return
        await asyncio.gather(*(self._execute_once(job) for job in due_jobs))

    async def _execute_once(self, job: CronJob) -> None:
        async with self._semaphore:
            lease_key = f"cron:{job.id}"
            if not self.lease_repo.acquire("cron-job", lease_key, ttl_s=self.claim_ttl_s):
                return
            try:
                latest = self.repository.get_job(job.id)
                if latest is None or not latest.enabled:
                    return
                if latest.state.next_run_at_ms is None or latest.state.next_run_at_ms > now_ms():
                    return
                started_ms = now_ms()
                logger.info("Cron: executing job '{}' ({})", latest.name, latest.id)
                try:
                    outcome = None
                    if self.on_job is not None:
                        outcome = await self.on_job(latest)
                    status = "queued" if outcome == "queued" else "ok"
                    self.repository.finalize_job_run(
                        latest.id,
                        started_ms=started_ms,
                        status=status,
                        error=None,
                    )
                    logger.info("Cron: job '{}' completed", latest.name)
                except Exception as exc:
                    logger.error("Cron: job '{}' failed: {}", latest.name, exc)
                    self.repository.finalize_job_run(
                        latest.id,
                        started_ms=started_ms,
                        status="error",
                        error=str(exc),
                    )
            finally:
                self.lease_repo.release("cron-job", lease_key)

    async def run_job(self, job_id: str, force: bool = False) -> bool:
        job = self.repository.get_job(job_id)
        if job is None:
            return False
        if not force and not job.enabled:
            return False
        await self._execute_once(job)
        return True

    def status(self) -> dict[str, int | bool | None]:
        jobs = self.repository.list_jobs(include_disabled=True)
        return {
            "enabled": self._running,
            "jobs": len(jobs),
            "next_wake_at_ms": self.repository.next_wake_at_ms(),
        }
