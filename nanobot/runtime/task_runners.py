"""Shared task execution helpers for runtime services."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.postprocess import render_postprocess_prompt
from nanobot.bus.events import OutboundMessage
from nanobot.cron.executor import execute_cron_job
from nanobot.heartbeat.scheduler import HeartbeatTarget
from nanobot.runtime.task_protocol import TaskEnvelope, make_consumer_name
from nanobot.runtime.task_queue import TaskMessage
from nanobot.runtime.task_serialization import (
    deserialize_cron_job,
    deserialize_deferred_actions,
    deserialize_outbound_message,
)


async def execute_postprocess_task(services: Any, task: TaskEnvelope) -> None:
    payload = task.payload
    actions = deserialize_deferred_actions(payload["actions"])
    if services.postprocess_agent_loop is None:
        raise RuntimeError("postprocess agent loop is not configured")
    response = await services.postprocess_agent_loop.process_direct(
        render_postprocess_prompt(
            actions,
            origin_user_message=str(payload["origin_user_message"]),
            assistant_reply=str(payload["assistant_reply"]),
        ),
        session_key=f"postprocess:{payload['session_key']}",
        channel=str(payload["channel"]),
        chat_id=str(payload["chat_id"]),
        tenant_key=payload.get("tenant_key"),
        session_type="postprocess",
        origin_session_key=str(payload["session_key"]),
        lane="subagent",
    )
    if payload.get("debug_trace") and str(payload.get("channel") or "") == "api":
        await services.emit_debug_trace(
            tenant_key=payload.get("tenant_key"),
            session_key=payload.get("session_key"),
            chat_id=payload.get("chat_id"),
            stage="completed",
            source="postprocess",
            content=(
                "后续写操作已完成。\n\n"
                f"动作：{json.dumps(payload['actions'], ensure_ascii=False, indent=2)}\n\n"
                f"执行结果：{response or '(empty)'}"
            ),
            extra={"task_id": task.task_id},
        )


async def execute_structured_memory_task(services: Any, task: TaskEnvelope) -> None:
    payload = task.payload
    tenant_key = payload.get("tenant_key") or task.tenant_key
    runtime = services.agent_loop._get_runtime(tenant_key)
    await services.agent_loop._structured_memory_manager.execute(
        runtime=runtime,
        session_key=str(payload["session_key"]),
        origin_user_message=str(payload["origin_user_message"]),
        assistant_reply=str(payload["assistant_reply"]),
        recent_messages=list(payload.get("recent_messages") or []),
    )
    if payload.get("debug_trace") and str(payload.get("channel") or "") == "api":
        await services.emit_debug_trace(
            tenant_key=tenant_key,
            session_key=str(payload["session_key"]),
            chat_id=payload.get("chat_id"),
            stage="completed",
            source="structured_memory",
            content=(
                "结构化记忆已完成。\n\n"
                "最新 MEMORY 快照：\n"
                f"{runtime.context.memory.read_long_term()}"
            ),
            extra={"task_id": task.task_id},
        )


async def execute_heartbeat_task(services: Any, task: TaskEnvelope) -> None:
    payload = task.payload
    target = HeartbeatTarget(
        tenant_key=str(payload["tenant_key"]),
        session_key=str(payload["session_key"]),
        channel=str(payload["channel"]),
        chat_id=str(payload["chat_id"]),
    )
    content = str(payload["content"])
    from nanobot.agent.tools.message import MessageTool

    action, tasks = await services.heartbeat_decider.decide_heartbeat(
        content,
        session_key=target.session_key,
        channel=target.channel,
        chat_id=target.chat_id,
        tenant_key=target.tenant_key,
    )
    if action != "run":
        state = services.tenant_state_repo.get(target.tenant_key)
        next_run = (
            state.heartbeat.next_run_at_ms
            if state is not None and state.heartbeat.next_run_at_ms is not None
            else None
        )
        if next_run is not None:
            services.tenant_state_repo.mark_heartbeat_result(
                target.tenant_key,
                status=action,
                next_run_at_ms=next_run,
                error=None,
            )
        return

    async def _silent(*_args, **_kwargs):
        return None

    response = await services.agent_loop.process_direct(
        (
            "[Heartbeat Trigger]\n"
            "This turn was triggered by the periodic heartbeat for the active session.\n\n"
            "## Decided Tasks\n"
            f"{tasks}"
        ),
        session_key=target.session_key,
        channel=target.channel,
        chat_id=target.chat_id,
        tenant_key=target.tenant_key,
        on_progress=_silent,
        extra_system_sections=[
            "# Heartbeat Context\n"
            "The following context was injected by the periodic heartbeat for this active session.\n\n"
            "## HEARTBEAT.md\n"
            f"{content}",
            "# Heartbeat Execution Rules\n"
            "Only act on the injected `HEARTBEAT.md` content and the `Decided Tasks` in the current user message.\n"
            "Do not bring in unrelated cron jobs, past reminders, or other session tasks unless they are explicitly included above.\n"
            "If the scope is narrow, keep the reply narrow.",
        ],
        lane="heartbeat",
        suppress_history=True,
    )
    message_tool = services.agent_loop.tools.get("message")
    if response and not (
        isinstance(message_tool, MessageTool) and message_tool.sent_in_turn()
    ):
        await services.enqueue_outbound_message(
            OutboundMessage(
                channel=target.channel,
                chat_id=target.chat_id,
                content=response,
                tenant_key=target.tenant_key,
                session_key=target.session_key,
                metadata={
                    "_event": "heartbeat_message",
                    "_source": "heartbeat",
                },
            )
        )
    state = services.tenant_state_repo.get(target.tenant_key)
    next_run = (
        state.heartbeat.next_run_at_ms
        if state is not None and state.heartbeat.next_run_at_ms is not None
        else None
    )
    if next_run is not None:
        services.tenant_state_repo.mark_heartbeat_result(
            target.tenant_key,
            status="ok",
            next_run_at_ms=next_run,
            error=None,
        )


async def execute_background_task(services: Any, task: TaskEnvelope) -> None:
    if task.task_type == "cron_fire":
        payload = task.payload
        job = deserialize_cron_job(payload["job"])
        await execute_cron_job(
            agent_loop=services.cron_agent_loop,
            job=job,
            default_channel=str(payload["default_channel"]),
            publish_outbound=services.enqueue_outbound_message,
        )
        return
    if task.task_type == "heartbeat_fire":
        await execute_heartbeat_task(services, task)
        return
    raise RuntimeError(f"Unsupported background task type: {task.task_type}")


async def execute_outbound_emit(
    _services: Any,
    task: TaskEnvelope,
    *,
    handler: Callable[[OutboundMessage], Awaitable[None]],
) -> None:
    if task.task_type != "outbound_emit":
        raise RuntimeError(f"Unsupported outbound task type: {task.task_type}")
    await handler(deserialize_outbound_message(task.payload))


async def handle_consumer_failure(
    services: Any,
    *,
    message: TaskMessage,
    consumer_group: str,
    consumer_name: str,
    error: Exception,
) -> None:
    task = message.task
    next_attempt = task.attempt + 1
    dead_letter = (
        next_attempt >= task.max_attempts
        and services.config.runtime.tasks.dead_letter_on_max_retry
    )
    if dead_letter and services.task_repository is not None:
        services.task_repository.mark_failed(task, error=str(error), dead_letter=True)
    elif services.task_repository is not None:
        services.task_repository.mark_failed(task, error=str(error), dead_letter=False)

    if dead_letter:
        envelope = TaskEnvelope(
            task_type="dead_letter",
            stream="dead-letter",
            tenant_key=task.tenant_key,
            session_key=task.session_key,
            trace_id=task.trace_id,
            max_attempts=task.max_attempts,
            service_role=services.role,
            payload={"original_task": task.to_dict(), "error": str(error)},
        )
        await services.enqueue_task(envelope)
    else:
        retry_task = TaskEnvelope(
            task_type=task.task_type,
            stream=task.stream,
            tenant_key=task.tenant_key,
            session_key=task.session_key,
            trace_id=task.trace_id,
            attempt=next_attempt,
            max_attempts=task.max_attempts,
            service_role=services.role,
            payload=task.payload,
        )
        await services.enqueue_task(retry_task)

    if task.payload.get("debug_trace") and str(task.payload.get("channel") or "") == "api":
        await services.emit_debug_trace(
            tenant_key=task.tenant_key,
            session_key=task.session_key,
            chat_id=task.payload.get("chat_id"),
            stage="failed",
            source=task.task_type,
            content=f"异步任务执行失败：{error}",
            extra={"task_id": task.task_id, "attempt": next_attempt, "dead_letter": dead_letter},
        )
    await services.task_queue.ack(message, consumer_group=consumer_group)
    logger.exception("Task failed for consumer {}: {}", consumer_name, error)


async def run_worker_loop(
    services: Any,
    *,
    stream: str,
    consumer_group: str,
    executor: Callable[[TaskEnvelope], Awaitable[None]],
) -> None:
    consumer_name = make_consumer_name(services.role)
    while True:
        messages = await services.task_queue.consume(
            stream,
            consumer_group=consumer_group,
            consumer_name=consumer_name,
            count=services.config.runtime.redis.consumer_batch_size,
        )
        for message in messages:
            task = message.task
            if services.task_repository is not None:
                services.task_repository.mark_claimed(
                    task,
                    consumer_name=consumer_name,
                    attempt=task.attempt + 1,
                )
            try:
                await executor(task)
                await services.task_queue.ack(message, consumer_group=consumer_group)
                if services.task_repository is not None:
                    services.task_repository.mark_succeeded(task)
            except Exception as exc:
                await handle_consumer_failure(
                    services,
                    message=message,
                    consumer_group=consumer_group,
                    consumer_name=consumer_name,
                    error=exc,
                )
