"""Multi-tenant heartbeat scheduler."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal

from loguru import logger

from simpleclaw.config.paths import get_tenant_workspace_path
from simpleclaw.storage.interfaces import LeaseStore, TenantStateStore
from simpleclaw.tenant.state import TenantState


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


@dataclass
class HeartbeatTarget:
    """One tenant primary session targeted by heartbeat."""

    tenant_key: str
    session_key: str
    channel: str
    chat_id: str


HeartbeatStatus = Literal["ok", "skip", "defer", "error", "queued"]
HeartbeatRunner = Callable[[HeartbeatTarget, str], Awaitable[HeartbeatStatus]]


class HeartbeatScheduler:
    """Scan due tenants and run heartbeat decisions per tenant."""

    def __init__(
        self,
        *,
        workspace: Path,
        tenant_repo: TenantStateStore,
        lease_repo: LeaseStore,
        runner: HeartbeatRunner,
        document_store: object | None = None,
        poll_interval_s: int = 15,
        busy_defer_s: int = 5 * 60,
        recent_activity_cooldown_s: int = 2 * 60,
        max_concurrency: int = 4,
        stagger_s: int = 0,
        tenant_filter: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.workspace = workspace
        self.tenant_repo = tenant_repo
        self.lease_repo = lease_repo
        self.runner = runner
        self.document_store = document_store
        self.poll_interval_s = max(1, poll_interval_s)
        self.busy_defer_s = max(1, busy_defer_s)
        self.recent_activity_cooldown_s = max(0, recent_activity_cooldown_s)
        self.max_concurrency = max(1, max_concurrency)
        self.stagger_s = max(0, stagger_s)
        self.tenant_filter = tenant_filter
        self.enabled = enabled
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    @staticmethod
    def _read_file(path: Path) -> str | None:
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def _build_heartbeat_content(self, tenant_key: str) -> str | None:
        sections: list[str] = []

        tenant_workspace = get_tenant_workspace_path(self.workspace, tenant_key, create=False)
        override_content = None
        if self.document_store is not None:
            getter = getattr(self.document_store, "get_active_content", None)
            if callable(getter):
                override_content = getter(tenant_key, "heartbeat", "HEARTBEAT.md")
        override_file = tenant_workspace / "overrides" / "HEARTBEAT.md"
        if override_content is None and self.document_store is None:
            override_content = self._read_file(override_file)
        if override_content:
            sections.append(f"## Tenant Override HEARTBEAT.md\n\n{override_content}")
        elif self.document_store is None:
            legacy_file = tenant_workspace / "HEARTBEAT.md"
            legacy_content = self._read_file(legacy_file)
            if legacy_content:
                sections.append(f"## Tenant HEARTBEAT.md\n\n{legacy_content}")

        return "\n\n".join(sections) if sections else None

    async def start(self) -> None:
        """Start the tenant scheduler loop."""
        if not self.enabled:
            logger.info("Heartbeat scheduler disabled")
            return
        if self._running:
            logger.warning("Heartbeat scheduler already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat scheduler started (poll={}s)", self.poll_interval_s)

    def stop(self) -> None:
        """Stop the tenant scheduler."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._scan_once()
                await asyncio.sleep(self.poll_interval_s)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Heartbeat scheduler tick failed")

    async def _scan_once(self) -> None:
        now_ms = _now_ms()
        due_states = self.tenant_repo.list_due_heartbeat_tenants(
            now_ms=now_ms,
            tenant_filter=self.tenant_filter,
        )
        if not due_states:
            return
        await asyncio.gather(*(self._run_for_tenant(state) for state in due_states))

    async def _run_for_tenant(self, state: TenantState) -> None:
        async with self._semaphore:
            lease_key = f"heartbeat:{state.tenant_key}"
            if not self.lease_repo.acquire("heartbeat-tenant", lease_key, ttl_s=max(self.busy_defer_s, 60)):
                return
            try:
                content = self._build_heartbeat_content(state.tenant_key)
                if not content:
                    return
                await self._execute_state(state, content)
            finally:
                self.lease_repo.release("heartbeat-tenant", lease_key)

    async def _execute_state(self, state: TenantState, content: str) -> None:
        now_ms = _now_ms()
        if not state.primary_session_key or not state.primary_channel or not state.primary_chat_id:
            self.tenant_repo.mark_heartbeat_result(
                state.tenant_key,
                status="skip",
                next_run_at_ms=now_ms + state.heartbeat.interval_s * 1000,
                error="No primary session configured",
                ran_at_ms=now_ms,
            )
            return

        if self._is_recently_active(state, now_ms):
            self.tenant_repo.mark_heartbeat_result(
                state.tenant_key,
                status="defer",
                next_run_at_ms=now_ms + self.busy_defer_s * 1000,
                error="Recent user activity",
                ran_at_ms=now_ms,
            )
            return

        routing_key = f"{state.tenant_key}:{state.primary_session_key}"
        if self.lease_repo.is_active("session-busy", routing_key):
            self.tenant_repo.mark_heartbeat_result(
                state.tenant_key,
                status="defer",
                next_run_at_ms=now_ms + self.busy_defer_s * 1000,
                error="Primary session is busy",
                ran_at_ms=now_ms,
            )
            return

        target = HeartbeatTarget(
            tenant_key=state.tenant_key,
            session_key=state.primary_session_key,
            channel=state.primary_channel,
            chat_id=state.primary_chat_id,
        )
        try:
            result = await self.runner(target, content)
        except Exception as exc:
            logger.exception("Heartbeat runner failed for tenant {}", state.tenant_key)
            self.tenant_repo.mark_heartbeat_result(
                state.tenant_key,
                status="error",
                next_run_at_ms=now_ms + self.busy_defer_s * 1000,
                error=str(exc),
                ran_at_ms=now_ms,
            )
            return

        next_run = (
            now_ms + self.busy_defer_s * 1000
            if result == "defer"
            else self._compute_next_regular_run(state, now_ms)
        )
        self.tenant_repo.mark_heartbeat_result(
            state.tenant_key,
            status=result,
            next_run_at_ms=next_run,
            error=None,
            ran_at_ms=now_ms,
        )

    def _is_recently_active(self, state: TenantState, now_ms: int) -> bool:
        if self.recent_activity_cooldown_s <= 0:
            return False
        if state.last_user_activity_at_ms is None:
            return False
        return (now_ms - state.last_user_activity_at_ms) < self.recent_activity_cooldown_s * 1000

    def _compute_next_regular_run(self, state: TenantState, now_ms: int) -> int:
        """Keep heartbeat cadence stable instead of always re-aligning to `now`."""
        scheduled_base = state.heartbeat.next_run_at_ms or now_ms
        base_ms = max(now_ms, scheduled_base)
        return base_ms + state.heartbeat.interval_s * 1000
