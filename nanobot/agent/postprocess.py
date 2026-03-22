"""Asynchronous postprocess execution for deferred write actions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.runtime import TenantRuntime
from nanobot.agent.tools.registry import ToolRegistry


@dataclass(slots=True)
class DeferredToolAction:
    """One tool action captured during the main turn for later execution."""

    tool_name: str
    params: dict[str, Any]
    synthetic_result: str


PostprocessRunner = Callable[
    [TenantRuntime, str, str, str, str, str | None, list[DeferredToolAction], str, str, bool],
    Awaitable[None],
]


def render_postprocess_prompt(
    actions: list[DeferredToolAction],
    *,
    origin_user_message: str,
    assistant_reply: str,
) -> str:
    """Build a compact prompt for the dedicated postprocess agent."""
    payload = [
        {
            "tool": action.tool_name,
            "params": action.params,
        }
        for action in actions
    ]
    return (
        "[Deferred Postprocess Task]\n"
        "The user-facing reply has already been sent. Do not send any reply to the user.\n"
        "Use tools only to persist or schedule the requested follow-up actions.\n"
        "Prefer the provided structured actions, but you may read current files before editing them.\n\n"
        "## Origin User Message\n"
        f"{origin_user_message}\n\n"
        "## Already Sent Assistant Reply\n"
        f"{assistant_reply}\n\n"
        "## Deferred Actions JSON\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "When the persistence work is complete, respond with a short completion note."
    )


class PostprocessManager:
    """Replay deferred write actions after the main reply is ready."""

    def __init__(
        self,
        tools: ToolRegistry,
        set_tool_context: Callable[[TenantRuntime, str, str, str, str, str | None], None],
        *,
        enabled: bool = True,
        max_concurrency: int = 2,
        runner: PostprocessRunner | None = None,
    ) -> None:
        self._tools = tools
        self._set_tool_context = set_tool_context
        self._enabled = enabled
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._tasks: set[asyncio.Task[None]] = set()
        self._runner = runner

    @property
    def enabled(self) -> bool:
        """Whether deferred postprocess execution is enabled."""
        return self._enabled

    def set_runner(self, runner: PostprocessRunner | None) -> None:
        """Set or replace the background postprocess runner."""
        self._runner = runner

    def schedule(
        self,
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
        debug_trace: bool = False,
    ) -> None:
        """Run deferred actions in the background."""
        if not self._enabled or not actions:
            return
        task = asyncio.create_task(
            self._run_actions(
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
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_actions(
        self,
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
        async with self._semaphore:
            if self._runner is not None:
                try:
                    await self._runner(
                        runtime,
                        channel,
                        chat_id,
                        session_key,
                        tenant_key,
                        message_id,
                        actions,
                        origin_user_message,
                        assistant_reply,
                        debug_trace,
                    )
                    return
                except Exception:
                    logger.exception("Postprocess runner failed; falling back to direct execution")

            self._set_tool_context(runtime, channel, chat_id, session_key, tenant_key, message_id)
            for action in actions:
                try:
                    args_str = json.dumps(action.params, ensure_ascii=False)
                    logger.info("Postprocess action: {}({})", action.tool_name, args_str[:200])
                    result = await self._tools.execute(action.tool_name, action.params)
                    logger.info("Postprocess result [{}]: {}", action.tool_name, result[:200])
                except Exception:
                    logger.exception("Postprocess action failed: {}", action.tool_name)
