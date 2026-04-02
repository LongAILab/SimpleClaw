"""Shared runtime bootstrap for chat-api, scheduler, and background workers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from rich.console import Console

from simpleclaw.agent.postprocess import DeferredToolAction
from simpleclaw.bus.events import OutboundMessage
from simpleclaw.bus.queue import MessageBus
from simpleclaw.config.paths import get_cron_dir
from simpleclaw.config.schema import Config
from simpleclaw.cron.service import CronService
from simpleclaw.cron.types import CronJob
from simpleclaw.heartbeat.scheduler import HeartbeatScheduler, HeartbeatTarget
from simpleclaw.runtime.agent_factory import make_agent_loop
from simpleclaw.runtime.config_runtime import (
    load_runtime_config,
    print_deprecated_memory_window_notice,
)
from simpleclaw.runtime.storage_factory import build_persistence_backends, build_queue_backends
from simpleclaw.runtime.task_protocol import TaskEnvelope
from simpleclaw.runtime.task_queue import InMemoryTaskQueue, RedisTaskQueue
from simpleclaw.runtime.task_runners import (
    execute_background_task,
    execute_heartbeat_task,
    execute_outbound_emit,
    execute_postprocess_task,
    execute_structured_memory_task,
    handle_consumer_failure,
    run_worker_loop,
)
from simpleclaw.runtime.task_serialization import (
    serialize_cron_job,
    serialize_deferred_actions,
    serialize_outbound_message,
)
from simpleclaw.session.manager import SessionManager
from simpleclaw.session.mysql import MySQLSessionManager
from simpleclaw.storage.interfaces import LeaseStore, TaskStateStore, TenantStateStore
from simpleclaw.utils.helpers import sync_workspace_templates


@dataclass(slots=True)
class RuntimeServices:
    """Fully wired runtime dependencies for one service role."""

    role: str
    config: Config
    console: Console
    bus: MessageBus
    task_queue: InMemoryTaskQueue | RedisTaskQueue
    task_repository: TaskStateStore | None
    tenant_state_repo: TenantStateStore
    lease_repo: LeaseStore
    cron_service: CronService
    agent_loop: Any
    cron_agent_loop: Any
    heartbeat_decider: Any
    postprocess_agent_loop: Any | None
    heartbeat_scheduler: HeartbeatScheduler
    create_session_manager: Callable[[Path, str], SessionManager]

    async def emit_debug_trace(
        self,
        *,
        tenant_key: str | None,
        session_key: str | None,
        chat_id: str | None,
        content: str,
        stage: str,
        source: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        target_chat_id = str(chat_id or session_key or "direct")
        metadata: dict[str, Any] = {
            "_event": "debug_trace",
            "_stage": stage,
            "_source": source,
        }
        if extra:
            metadata.update(extra)
        await self.enqueue_outbound_message(
            OutboundMessage(
                channel="api",
                chat_id=target_chat_id,
                content=content,
                tenant_key=tenant_key,
                session_key=session_key,
                metadata=metadata,
            )
        )

    async def enqueue_task(self, task: TaskEnvelope) -> str:
        queue_id = await self.task_queue.enqueue(task)
        if self.task_repository is not None:
            self.task_repository.enqueue(task, queue_message_id=queue_id)
        return queue_id

    async def enqueue_postprocess_actions(
        self,
        runtime,
        channel: str,
        chat_id: str,
        session_key: str,
        tenant_key: str,
        message_id: str | None,
        actions: list[DeferredToolAction],
        origin_user_message: str,
        assistant_reply: str,
        debug_trace: bool,
    ) -> None:
        del runtime
        serialized = serialize_deferred_actions(actions)
        task = TaskEnvelope(
            task_type="postprocess_actions",
            stream="postprocess",
            tenant_key=tenant_key,
            session_key=session_key,
            max_attempts=self.config.runtime.tasks.max_retry,
            service_role=self.role,
            payload={
                "channel": channel,
                "chat_id": chat_id,
                "session_key": session_key,
                "tenant_key": tenant_key,
                "message_id": message_id,
                "actions": serialized,
                "origin_user_message": origin_user_message,
                "assistant_reply": assistant_reply,
                "debug_trace": debug_trace,
            },
        )
        await self.enqueue_task(task)
        if debug_trace and channel == "api":
            await self.emit_debug_trace(
                tenant_key=tenant_key,
                session_key=session_key,
                chat_id=chat_id,
                stage="queued",
                source="postprocess",
                content=(
                    "已排队后续写操作。\n\n"
                    f"{json.dumps(serialized, ensure_ascii=False, indent=2)}"
                ),
                extra={"action_count": len(actions)},
            )

    async def enqueue_structured_memory(
        self,
        runtime,
        channel: str,
        chat_id: str,
        session_key: str,
        origin_user_message: str,
        assistant_reply: str,
        recent_messages: list[dict[str, Any]],
        debug_trace: bool,
        skip_memory_items: bool = False,
    ) -> None:
        task = TaskEnvelope(
            task_type="structured_memory_extract",
            stream="postprocess",
            tenant_key=getattr(runtime, "tenant_key", None),
            session_key=session_key,
            max_attempts=self.config.runtime.tasks.max_retry,
            service_role=self.role,
            payload={
                "session_key": session_key,
                "channel": channel,
                "chat_id": chat_id,
                "origin_user_message": origin_user_message,
                "assistant_reply": assistant_reply,
                "recent_messages": recent_messages,
                "debug_trace": debug_trace,
                "skip_memory_items": skip_memory_items,
            },
        )
        await self.enqueue_task(task)
        if debug_trace and channel == "api":
            await self.emit_debug_trace(
                tenant_key=getattr(runtime, "tenant_key", None),
                session_key=session_key,
                chat_id=chat_id,
                stage="queued",
                source="structured_memory",
                content=(
                    "已排队结构化记忆提取。\n\n"
                    f"用户消息：{origin_user_message}\n\n"
                    f"已发送回复：{assistant_reply}"
                ),
            )

    async def enqueue_cron_fire(self, job: CronJob, default_channel: str) -> str:
        task = TaskEnvelope(
            task_type="cron_fire",
            stream="background",
            tenant_key=job.payload.tenant_key,
            session_key=job.payload.session_key,
            max_attempts=self.config.runtime.tasks.max_retry,
            service_role=self.role,
            payload={
                "job": serialize_cron_job(job),
                "default_channel": default_channel,
            },
        )
        await self.enqueue_task(task)
        return "queued"

    async def enqueue_heartbeat_fire(self, target: HeartbeatTarget, content: str) -> str:
        task = TaskEnvelope(
            task_type="heartbeat_fire",
            stream="background",
            tenant_key=target.tenant_key,
            session_key=target.session_key,
            max_attempts=self.config.runtime.tasks.max_retry,
            service_role=self.role,
            payload={
                "tenant_key": target.tenant_key,
                "session_key": target.session_key,
                "channel": target.channel,
                "chat_id": target.chat_id,
                "content": content,
            },
        )
        await self.enqueue_task(task)
        return "queued"

    async def enqueue_outbound_message(self, message: OutboundMessage) -> str:
        task = TaskEnvelope(
            task_type="outbound_emit",
            stream="outbound",
            tenant_key=message.tenant_key,
            session_key=message.session_key,
            max_attempts=self.config.runtime.tasks.max_retry,
            service_role=self.role,
            payload=serialize_outbound_message(message),
        )
        return await self.enqueue_task(task)

    async def execute_postprocess_task(self, task: TaskEnvelope) -> None:
        await execute_postprocess_task(self, task)

    async def execute_structured_memory_task(self, task: TaskEnvelope) -> None:
        await execute_structured_memory_task(self, task)

    async def execute_heartbeat_task(self, task: TaskEnvelope) -> None:
        await execute_heartbeat_task(self, task)

    async def execute_background_task(self, task: TaskEnvelope) -> None:
        await execute_background_task(self, task)

    async def run_outbound_worker(
        self,
        handler: Callable[[OutboundMessage], Awaitable[None]],
        *,
        consumer_group: str,
    ) -> None:
        async def _executor(task: TaskEnvelope) -> None:
            await execute_outbound_emit(self, task, handler=handler)

        await run_worker_loop(
            self,
            stream="outbound",
            consumer_group=consumer_group,
            executor=_executor,
        )

    async def _handle_consumer_failure(
        self,
        *,
        message,
        consumer_group: str,
        consumer_name: str,
        error: Exception,
    ) -> None:
        await handle_consumer_failure(
            self,
            message=message,
            consumer_group=consumer_group,
            consumer_name=consumer_name,
            error=error,
        )

    async def run_postprocess_worker(self) -> None:
        consumer_group = self.config.runtime.tasks.consumer_group

        async def _executor(task: TaskEnvelope) -> None:
            if task.task_type == "postprocess_actions":
                await self.execute_postprocess_task(task)
            elif task.task_type == "structured_memory_extract":
                await self.execute_structured_memory_task(task)
            else:
                raise RuntimeError(f"Unsupported postprocess task type: {task.task_type}")

        await run_worker_loop(
            self,
            stream="postprocess",
            consumer_group=consumer_group,
            executor=_executor,
        )

    async def run_background_worker(self) -> None:
        consumer_group = self.config.runtime.tasks.consumer_group
        await run_worker_loop(
            self,
            stream="background",
            consumer_group=consumer_group,
            executor=self.execute_background_task,
        )

    async def close(self) -> None:
        agents = [self.agent_loop, self.cron_agent_loop, self.heartbeat_decider, self.postprocess_agent_loop]
        for agent in agents:
            if agent is None:
                continue
            await agent.close_mcp()
            agent.stop()


def build_runtime_services(
    *,
    console: Console,
    role: str,
    config_path: str | None = None,
    workspace: str | None = None,
) -> RuntimeServices:
    """Build one fully wired runtime for the requested service role."""
    config = load_runtime_config(console, config_path=config_path, workspace=workspace)
    print_deprecated_memory_window_notice(config, console)
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    runtime_role = role or config.runtime.role
    persistence = build_persistence_backends(config)
    queue_backends = build_queue_backends(config)

    def create_context_builder(workspace_path: Path, tenant_key: str):
        from simpleclaw.agent.context import ContextBuilder

        return ContextBuilder(
            workspace_path,
            tenant_key=tenant_key,
            document_store=persistence.document_store,
            memory_store=persistence.memory_store,
            tenant_state_repo=persistence.tenant_state_repo,
            context_window_tokens=config.agents.defaults.context_window_tokens,
        )

    def provision_tenant(tenant_key: str) -> None:
        if persistence.tenant_provisioner is not None:
            persistence.tenant_provisioner.ensure_initialized(tenant_key)

    def create_session_manager(workspace_path: Path, tenant_key: str) -> SessionManager:
        if persistence.session_store is not None:
            return MySQLSessionManager(
                workspace_path,
                session_store=persistence.session_store,
                tenant_key=tenant_key,
            )
        return SessionManager(workspace_path, tenant_key=tenant_key)

    cron_service = CronService(
        get_cron_dir() / "jobs.json",
        lease_repo=queue_backends.lease_repo,
        enabled=config.gateway.cron.enabled,
        max_concurrency=config.gateway.lanes.cron,
        repository=persistence.cron_repository,
    )

    common_loop_kwargs = {
        "config": config,
        "console": console,
        "bus": bus,
        "tenant_state_repo": persistence.tenant_state_repo,
        "lease_repo": queue_backends.lease_repo,
        "create_session_manager": create_session_manager,
        "create_context_builder": create_context_builder,
        "provision_tenant": provision_tenant,
        "ensure_tenant_workspace": not config.runtime.mysql.enabled,
        "document_store": persistence.document_store,
        "memory_store": persistence.memory_store,
    }

    agent_loop = make_agent_loop(
        cron_service=cron_service,
        **common_loop_kwargs,
    )
    cron_agent_loop = (
        make_agent_loop(
            cron_service=cron_service,
            settings=config.agents.cron,
            **common_loop_kwargs,
        )
        if config.agents.cron.enabled
        else agent_loop
    )
    heartbeat_decider = (
        make_agent_loop(
            settings=config.agents.heartbeat,
            **common_loop_kwargs,
        )
        if config.agents.heartbeat.enabled
        else agent_loop
    )
    postprocess_agent_loop = (
        make_agent_loop(
            cron_service=cron_service,
            settings=config.agents.postprocess,
            **common_loop_kwargs,
        )
        if config.agents.postprocess.enabled
        else None
    )
    if postprocess_agent_loop is not None:
        postprocess_agent_loop.disable_deferred_postprocess()

    services: RuntimeServices

    async def heartbeat_queue_runner(target: HeartbeatTarget, content: str) -> str:
        return await services.enqueue_heartbeat_fire(target, content)

    services = RuntimeServices(
        role=runtime_role,
        config=config,
        console=console,
        bus=bus,
        task_queue=queue_backends.task_queue,
        task_repository=persistence.task_repository,
        tenant_state_repo=persistence.tenant_state_repo,
        lease_repo=queue_backends.lease_repo,
        cron_service=cron_service,
        agent_loop=agent_loop,
        cron_agent_loop=cron_agent_loop,
        heartbeat_decider=heartbeat_decider,
        postprocess_agent_loop=postprocess_agent_loop,
        heartbeat_scheduler=HeartbeatScheduler(
            workspace=config.workspace_path,
            tenant_repo=persistence.tenant_state_repo,
            lease_repo=queue_backends.lease_repo,
            runner=heartbeat_queue_runner,
            document_store=persistence.document_store,
            poll_interval_s=config.gateway.heartbeat.poll_interval_s,
            busy_defer_s=config.gateway.heartbeat.busy_defer_s,
            recent_activity_cooldown_s=config.gateway.heartbeat.recent_activity_cooldown_s,
            max_concurrency=config.gateway.lanes.heartbeat,
            stagger_s=config.gateway.heartbeat.stagger_s,
            tenant_filter=config.gateway.heartbeat.target_tenant_key,
            enabled=config.gateway.heartbeat.enabled,
        ),
        create_session_manager=create_session_manager,
    )
    if runtime_role == "background-worker":
        bus.publish_outbound = services.enqueue_outbound_message  # type: ignore[method-assign]
        for loop in (agent_loop, cron_agent_loop, postprocess_agent_loop):
            if loop is None:
                continue
            tool = loop.tools.get("message")
            if tool is not None and hasattr(tool, "set_send_callback"):
                tool.set_send_callback(services.enqueue_outbound_message)
    if config.runtime.redis.enabled:
        agent_loop.set_postprocess_runner(services.enqueue_postprocess_actions)
        agent_loop.set_structured_memory_runner(services.enqueue_structured_memory)
    return services


async def run_scheduler_service(services: RuntimeServices) -> None:
    """Run cron/heartbeat scanners as a dedicated service."""

    async def on_cron_job(job: CronJob) -> str | None:
        return await services.enqueue_cron_fire(job, default_channel="api")

    services.cron_service.on_job = on_cron_job
    await services.cron_service.start()
    await services.heartbeat_scheduler.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        services.heartbeat_scheduler.stop()
        services.cron_service.stop()

