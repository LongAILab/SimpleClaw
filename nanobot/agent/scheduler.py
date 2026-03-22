"""Session mailbox and worker scheduling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from nanobot.bus.events import InboundMessage, OutboundMessage

ProcessCallback = Callable[
    [InboundMessage, Callable[[str], Awaitable[None]] | None],
    Awaitable[OutboundMessage | None],
]
PublishCallback = Callable[[OutboundMessage], Awaitable[None]]
TaskHook = Callable[[InboundMessage], Awaitable[None]]


@dataclass
class SessionEnvelope:
    """One pending message routed to a session mailbox."""

    msg: InboundMessage
    future: asyncio.Future[OutboundMessage | None] | None = None
    on_progress: Callable[[str], Awaitable[None]] | None = None


class SessionScheduler:
    """Own session mailboxes, workers, and turn cancellation."""

    def __init__(
        self,
        process_message: ProcessCallback,
        publish_outbound: PublishCallback,
        idle_timeout_s: float = 30.0,
        on_task_start: TaskHook | None = None,
        on_task_end: TaskHook | None = None,
        lane_limits: dict[str, int] | None = None,
        lane_backlog_caps: dict[str, int] | None = None,
        lane_tenant_limits: dict[str, int] | None = None,
    ) -> None:
        self._process_message = process_message
        self._publish_outbound = publish_outbound
        self._idle_timeout_s = idle_timeout_s
        self._on_task_start = on_task_start
        self._on_task_end = on_task_end
        self._lane_limits = {name: max(1, value) for name, value in (lane_limits or {}).items()}
        self._lane_backlog_caps = {name: max(1, value) for name, value in (lane_backlog_caps or {}).items()}
        self._lane_tenant_limits = {name: max(1, value) for name, value in (lane_tenant_limits or {}).items()}
        self._lane_semaphores: dict[str, asyncio.Semaphore] = {}
        self._tenant_lane_semaphores: dict[tuple[str, str], asyncio.Semaphore] = {}
        self._lane_pending_counts: dict[str, int] = {}
        self.active_tasks: dict[str, list[asyncio.Task]] = {}
        self.session_mailboxes: dict[str, asyncio.Queue[SessionEnvelope]] = {}
        self.session_workers: dict[str, asyncio.Task[None]] = {}

    async def submit(self, envelope: SessionEnvelope) -> OutboundMessage | None:
        """Route a message into its session mailbox and optionally await completion."""
        rejected = await self._maybe_backpressure(envelope)
        if rejected is not None:
            return rejected
        routing_key = envelope.msg.routing_key
        queue = self.session_mailboxes.setdefault(routing_key, asyncio.Queue())
        lane = envelope.msg.lane
        self._lane_pending_counts[lane] = self._lane_pending_counts.get(lane, 0) + 1
        await queue.put(envelope)
        self._ensure_session_worker(routing_key)
        if envelope.future is None:
            return None
        return await envelope.future

    async def cancel_session(self, routing_key: str) -> tuple[int, int]:
        """Cancel active turns and drop queued turns for one session."""
        tasks = self.active_tasks.pop(routing_key, [])
        cancelled = sum(1 for task in tasks if not task.done() and task.cancel())
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        dropped = 0
        queue = self.session_mailboxes.get(routing_key)
        if queue is not None:
            while not queue.empty():
                try:
                    envelope = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                dropped += 1
                lane = envelope.msg.lane
                self._lane_pending_counts[lane] = max(0, self._lane_pending_counts.get(lane, 0) - 1)
                if envelope.future is not None and not envelope.future.done():
                    envelope.future.cancel()

        return cancelled, dropped

    def stop(self) -> None:
        """Stop all active session workers."""
        for worker in self.session_workers.values():
            worker.cancel()

    def get_session_load(self, routing_key: str) -> dict[str, int | bool]:
        """Return active/queued state for one session mailbox."""
        queue = self.session_mailboxes.get(routing_key)
        queued = queue.qsize() if queue is not None else 0
        active = len(self.active_tasks.get(routing_key, []))
        return {
            "active": active > 0,
            "active_count": active,
            "queued_count": queued,
        }

    def _ensure_session_worker(self, routing_key: str) -> None:
        """Ensure one worker is alive for the given routing key."""
        worker = self.session_workers.get(routing_key)
        if worker is None or worker.done():
            self.session_workers[routing_key] = asyncio.create_task(
                self._session_worker(routing_key)
            )

    def _get_lane_semaphore(self, lane: str) -> asyncio.Semaphore | None:
        """Return the semaphore controlling one execution lane."""
        limit = self._lane_limits.get(lane)
        if limit is None:
            return None
        if lane not in self._lane_semaphores:
            self._lane_semaphores[lane] = asyncio.Semaphore(limit)
        return self._lane_semaphores[lane]

    def _get_tenant_lane_semaphore(self, lane: str, tenant_key: str) -> asyncio.Semaphore | None:
        """Return the semaphore controlling one tenant inside a lane."""
        limit = self._lane_tenant_limits.get(lane)
        if limit is None:
            return None
        key = (lane, tenant_key)
        if key not in self._tenant_lane_semaphores:
            self._tenant_lane_semaphores[key] = asyncio.Semaphore(limit)
        return self._tenant_lane_semaphores[key]

    async def _maybe_backpressure(self, envelope: SessionEnvelope) -> OutboundMessage | None:
        """Reject new work when a lane backlog cap is exceeded."""
        lane = envelope.msg.lane
        cap = self._lane_backlog_caps.get(lane)
        pending = self._lane_pending_counts.get(lane, 0)
        if cap is None or pending < cap:
            return None
        response = OutboundMessage(
            channel=envelope.msg.channel,
            chat_id=envelope.msg.chat_id,
            content="System is busy right now. Please retry in a moment.",
            metadata=envelope.msg.metadata or {},
        )
        if envelope.future is not None:
            if not envelope.future.done():
                envelope.future.set_result(response)
            return response
        if envelope.msg.channel != "system":
            await self._publish_outbound(response)
        return None

    async def _run_envelope_task(self, envelope: SessionEnvelope) -> OutboundMessage | None:
        """Execute one envelope under lane limits and lifecycle hooks."""
        if self._on_task_start is not None:
            await self._on_task_start(envelope.msg)
        semaphore = self._get_lane_semaphore(envelope.msg.lane)
        tenant_semaphore = self._get_tenant_lane_semaphore(
            envelope.msg.lane,
            envelope.msg.effective_tenant_key,
        )
        if semaphore is None and tenant_semaphore is None:
            return await self._process_message(envelope.msg, envelope.on_progress)
        if semaphore is None:
            async with tenant_semaphore:
                return await self._process_message(envelope.msg, envelope.on_progress)
        if tenant_semaphore is None:
            async with semaphore:
                return await self._process_message(envelope.msg, envelope.on_progress)
        async with semaphore:
            async with tenant_semaphore:
                return await self._process_message(envelope.msg, envelope.on_progress)

    async def _session_worker(self, routing_key: str) -> None:
        """Consume one session mailbox sequentially until it has been idle long enough."""
        queue = self.session_mailboxes[routing_key]
        current_task = asyncio.current_task()

        while True:
            try:
                envelope = await asyncio.wait_for(queue.get(), timeout=self._idle_timeout_s)
            except asyncio.TimeoutError:
                if queue.empty() and not self.active_tasks.get(routing_key):
                    if self.session_workers.get(routing_key) is current_task:
                        self.session_workers.pop(routing_key, None)
                        self.session_mailboxes.pop(routing_key, None)
                    return
                continue
            except asyncio.CancelledError:
                break

            task = asyncio.create_task(self._run_envelope_task(envelope))
            self.active_tasks.setdefault(routing_key, []).append(task)
            try:
                response = await task
                if envelope.future is not None:
                    if not envelope.future.done():
                        envelope.future.set_result(response)
                else:
                    if response is not None:
                        await self._publish_outbound(response)
                    elif envelope.msg.channel == "cli":
                        await self._publish_outbound(OutboundMessage(
                            channel=envelope.msg.channel,
                            chat_id=envelope.msg.chat_id,
                            content="",
                            metadata=envelope.msg.metadata or {},
                        ))
            except asyncio.CancelledError:
                if envelope.future is not None and not envelope.future.done():
                    envelope.future.cancel()
                continue
            finally:
                lane = envelope.msg.lane
                self._lane_pending_counts[lane] = max(0, self._lane_pending_counts.get(lane, 0) - 1)
                active = self.active_tasks.get(routing_key, [])
                if task in active:
                    active.remove(task)
                if not active:
                    self.active_tasks.pop(routing_key, None)
                if self._on_task_end is not None and not self.active_tasks.get(routing_key) and queue.empty():
                    await self._on_task_end(envelope.msg)
