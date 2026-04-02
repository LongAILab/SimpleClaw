"""Post-turn side effects for one completed turn."""

from __future__ import annotations

from typing import Any

from simpleclaw.agent.postprocess import DeferredToolAction, PostprocessManager
from simpleclaw.agent.runtime import TenantRuntime
from simpleclaw.agent.structured_memory import StructuredMemoryManager


def schedule_deferred_actions(
    manager: PostprocessManager | None,
    *,
    runtime: TenantRuntime,
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
    """Queue deferred postprocess actions when enabled."""
    if manager is None or not actions:
        return
    manager.schedule(
        runtime=runtime,
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
        tenant_key=tenant_key,
        message_id=message_id,
        actions=actions,
        origin_user_message=origin_user_message,
        assistant_reply=assistant_reply,
        debug_trace=debug_trace,
    )


def schedule_structured_memory(
    manager: StructuredMemoryManager | None,
    *,
    runtime: TenantRuntime,
    channel: str,
    chat_id: str,
    session_key: str,
    origin_user_message: str,
    assistant_reply: str,
    recent_messages: list[dict[str, Any]],
    debug_trace: bool,
    has_explicit_memory_write: bool,
) -> None:
    """Queue background structured-memory extraction when enabled."""
    if manager is None or not manager.enabled:
        return
    manager.schedule(
        runtime=runtime,
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
        origin_user_message=origin_user_message,
        assistant_reply=assistant_reply,
        recent_messages=recent_messages,
        debug_trace=debug_trace,
        skip_memory_items=has_explicit_memory_write,
    )
