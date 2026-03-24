"""Single-turn processing for the agent loop."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.postprocess import DeferredToolAction, PostprocessManager
from nanobot.agent.turn_commit import save_turn_messages
from nanobot.agent.turn_effects import schedule_deferred_actions, schedule_structured_memory
from nanobot.agent.runtime import TenantRuntime
from nanobot.agent.structured_memory import StructuredMemoryManager
from nanobot.agent.tools.cron import CRON_MUTATING_TOOL_NAMES
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.turn_utils import (
    apply_session_metadata,
    derive_session_type,
    get_extra_system_sections,
    get_history_for_turn,
    get_outbound_message_metadata,
    get_outbound_session_key,
    matches_deferred_path,
    strip_think,
    tool_hint,
)
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.session.manager import Session


class TurnProcessor:
    """Own the logic for one conversational turn."""

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        max_iterations: int,
        tools: ToolRegistry,
        bus: MessageBus,
        restrict_to_workspace: bool,
        tool_result_max_chars: int,
        default_workspace: Path,
        postprocess_manager: PostprocessManager | None = None,
        structured_memory_manager: StructuredMemoryManager | None = None,
        defer_cron_writes: bool = False,
        defer_heartbeat_writes: bool = False,
        defer_memory_writes: bool = False,
    ) -> None:
        self._provider = provider
        self._model = model
        self._max_iterations = max_iterations
        self._tools = tools
        self._bus = bus
        self._restrict_to_workspace = restrict_to_workspace
        self._tool_result_max_chars = tool_result_max_chars
        self._default_workspace = default_workspace
        self._postprocess_manager = postprocess_manager
        self._structured_memory_manager = structured_memory_manager
        self._defer_cron_writes = defer_cron_writes
        self._defer_heartbeat_writes = defer_heartbeat_writes
        self._defer_memory_writes = defer_memory_writes
        self._last_llm_debug: list[dict[str, Any]] = []

    @staticmethod
    def _get_extra_system_sections(metadata: dict[str, Any] | None) -> list[str] | None:
        """Extract transient system-prompt sections from message metadata."""
        return get_extra_system_sections(metadata)

    @staticmethod
    def _get_outbound_message_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        """Attach SSE event metadata for background lanes using message tool."""
        return get_outbound_message_metadata(metadata)

    @staticmethod
    def _get_outbound_session_key(metadata: dict[str, Any] | None, fallback: str) -> str:
        """Use UI-facing session key for outbound messages when provided."""
        return get_outbound_session_key(metadata, fallback)

    @staticmethod
    def _get_history_for_turn(
        session: Session,
        metadata: dict[str, Any] | None,
        history_selector: Callable[[Session], list[dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return session history unless this turn explicitly suppresses it."""
        return get_history_for_turn(session, metadata, history_selector)

    def set_tool_context(
        self,
        runtime: TenantRuntime,
        channel: str,
        chat_id: str,
        session_key: str,
        tenant_key: str,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        allowed_dir = runtime.workspace if self._restrict_to_workspace else None
        outbound_metadata = self._get_outbound_message_metadata(metadata)
        outbound_session_key = self._get_outbound_session_key(metadata, session_key)
        action_session_key = session_key
        if metadata:
            origin_session_key = metadata.get("_origin_session_key")
            if isinstance(origin_session_key, str) and origin_session_key.strip():
                action_session_key = origin_session_key.strip()
        for name in ("read_file", "write_file", "edit_file", "list_dir"):
            if tool := self._tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(runtime.workspace, allowed_dir, tenant_key=tenant_key)
        if tool := self._tools.get("exec"):
            if hasattr(tool, "set_context"):
                tool.set_context(str(runtime.workspace))
        for name in ("message", "spawn", "cron"):
            if tool := self._tools.get(name):
                if hasattr(tool, "set_context"):
                    if name == "message":
                        tool.set_context(
                            channel,
                            chat_id,
                            message_id,
                            session_key=outbound_session_key,
                            tenant_key=tenant_key,
                            metadata=outbound_metadata,
                        )
                    elif name == "spawn":
                        tool.set_context(
                            channel,
                            chat_id,
                            session_key=session_key,
                            tenant_key=tenant_key,
                            manager=runtime.subagents,
                        )
                    else:
                        tool.set_context(
                            channel,
                            chat_id,
                            session_key=action_session_key,
                            tenant_key=tenant_key,
                        )

    @staticmethod
    def strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        return strip_think(text)

    @staticmethod
    def tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        return tool_hint(tool_calls)

    @staticmethod
    def _matches_deferred_path(path: Any, expected_suffix: str) -> bool:
        """Check whether a tool path targets one deferred-write file."""
        return matches_deferred_path(path, expected_suffix)

    @staticmethod
    def _derive_session_type(metadata: dict[str, Any] | None, session_key: str) -> str:
        """Resolve the persisted session type for this turn."""
        return derive_session_type(metadata, session_key)

    def _apply_session_metadata(
        self,
        session: Session,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        """Persist stable routing/session semantics onto the session object."""
        apply_session_metadata(
            session,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            metadata=metadata,
        )

    def _prepare_deferred_action(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> DeferredToolAction | None:
        """Convert selected write actions into deferred postprocess work."""
        if self._postprocess_manager is None or not self._postprocess_manager.enabled:
            return None

        if tool_name in CRON_MUTATING_TOOL_NAMES and self._defer_cron_writes:
            deferred_params = dict(params)
            cron_tool = self._tools.get(tool_name)
            if hasattr(cron_tool, "preflight"):
                normalized_params, error = cron_tool.preflight(dict(params))
                if error:
                    return None
                if normalized_params is not None:
                    deferred_params = normalized_params
            return DeferredToolAction(
                tool_name=tool_name,
                params=deferred_params,
                synthetic_result=(
                    "Validated cron change and queued it for deferred postprocess persistence. "
                    "Tell the user it has been accepted for background saving, and do not claim the job "
                    "has already been created. Do not call cron again this turn."
                ),
            )

        if tool_name not in {"write_file", "edit_file"}:
            return None

        path = params.get("path")
        if self._defer_heartbeat_writes and self._matches_deferred_path(path, "overrides/HEARTBEAT.md"):
            return DeferredToolAction(
                tool_name=tool_name,
                params=dict(params),
                synthetic_result=(
                    "Accepted HEARTBEAT.md update for deferred postprocess execution. "
                    "Treat this request as queued successfully and do not rewrite the same file again this turn."
                ),
            )

        if self._defer_memory_writes and self._matches_deferred_path(path, "memory/MEMORY.md"):
            return DeferredToolAction(
                tool_name=tool_name,
                params=dict(params),
                synthetic_result=(
                    "Accepted MEMORY.md update for deferred postprocess execution. "
                    "Treat this request as queued successfully and do not rewrite the same file again this turn."
                ),
            )

        return None

    def disable_deferred_postprocess(self) -> None:
        """Disable postprocess-related write interception for dedicated background workers."""
        self._defer_cron_writes = False
        self._defer_heartbeat_writes = False
        self._defer_memory_writes = False
        if self._structured_memory_manager is not None:
            self._structured_memory_manager.disable()

    @classmethod
    def _has_explicit_memory_write(cls, actions: list[DeferredToolAction]) -> bool:
        """Return True when the main model already queued a direct MEMORY.md edit."""
        for action in actions:
            if action.tool_name not in {"write_file", "edit_file"}:
                continue
            if cls._matches_deferred_path(action.params.get("path"), "memory/MEMORY.md"):
                return True
        return False

    def get_last_llm_debug(self) -> list[dict[str, Any]]:
        """Return debug metadata for provider calls from the most recent turn."""
        return [dict(item) for item in self._last_llm_debug]

    @staticmethod
    def excluded_tool_names_for_lane(lane: str) -> set[str] | None:
        """Return tool names hidden from the model in one execution lane."""
        return {"message"} if lane == "main" else None

    @staticmethod
    def _filter_tool_definitions(
        tool_defs: list[dict[str, Any]],
        excluded_tool_names: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Remove selected tool schemas before exposing them to the model."""
        if not excluded_tool_names:
            return tool_defs
        return [
            tool_def
            for tool_def in tool_defs
            if str((tool_def.get("function") or {}).get("name") or "") not in excluded_tool_names
        ]

    def get_llm_tool_definitions(self, *, lane: str = "main") -> list[dict[str, Any]]:
        """Return the exact tool schemas exposed to the model for one lane."""
        return self._filter_tool_definitions(
            self._tools.get_definitions(),
            excluded_tool_names=self.excluded_tool_names_for_lane(lane),
        )

    async def run_agent_loop(
        self,
        initial_messages: list[dict],
        context_builder: ContextBuilder | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        deferred_actions: list[DeferredToolAction] | None = None,
        stream_text: bool = False,
        excluded_tool_names: set[str] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop."""
        builder = context_builder or ContextBuilder(self._default_workspace)
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        self._last_llm_debug = []

        while iteration < self._max_iterations:
            iteration += 1

            tool_defs = self._filter_tool_definitions(
                self._tools.get_definitions(),
                excluded_tool_names=excluded_tool_names,
            )
            response = await self._get_llm_response(
                messages=messages,
                tool_defs=tool_defs,
                on_progress=on_progress,
                stream_text=stream_text,
            )

            if response.has_tool_calls:
                if on_progress:
                    thought = self.strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self.tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    tc.to_openai_tool_call()
                    for tc in response.tool_calls
                ]
                messages = builder.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    deferred = self._prepare_deferred_action(tool_call.name, tool_call.arguments)
                    if deferred is not None:
                        if deferred_actions is not None:
                            deferred_actions.append(deferred)
                        result = deferred.synthetic_result
                    else:
                        result = await self._tools.execute(tool_call.name, tool_call.arguments)
                    messages = builder.add_tool_result(
                        messages,
                        tool_call.id,
                        tool_call.name,
                        result,
                    )
            else:
                clean = self.strip_think(response.content)
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = builder.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self._max_iterations:
            logger.warning("Max iterations ({}) reached", self._max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self._max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages

    async def _get_llm_response(
        self,
        *,
        messages: list[dict],
        tool_defs: list[dict[str, Any]],
        on_progress: Callable[..., Awaitable[None]] | None,
        stream_text: bool,
    ) -> LLMResponse:
        """Get one LLM response, optionally streaming text deltas."""
        if not stream_text or on_progress is None:
            response = await self._provider.chat_with_retry(
                messages=messages,
                tools=tool_defs,
                model=self._model,
            )
            self._last_llm_debug.append(
                {
                    "model": self._model,
                    "finish_reason": response.finish_reason,
                    "tool_call_count": len(response.tool_calls),
                    "usage": dict(response.usage or {}),
                }
            )
            return response

        final_response: LLMResponse | None = None
        async for event in self._provider.stream_chat_with_retry(
            messages=messages,
            tools=tool_defs,
            model=self._model,
        ):
            if event.event == "text_delta" and event.text:
                await on_progress(event.text, text_delta=True)
            elif event.event == "response":
                final_response = event.response

        if final_response is None:
            return LLMResponse(
                content="Error calling LLM: stream returned no final response",
                finish_reason="error",
            )
        self._last_llm_debug.append(
            {
                "model": self._model,
                "finish_reason": final_response.finish_reason,
                "tool_call_count": len(final_response.tool_calls),
                "usage": dict(final_response.usage or {}),
            }
        )
        return final_response

    async def dispatch(
        self,
        msg: InboundMessage,
        runtime: TenantRuntime,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message and return any final outbound response."""
        try:
            return await self.process_message(msg, runtime, on_progress=on_progress)
        except asyncio.CancelledError:
            logger.info("Task cancelled for session {}", msg.session_key)
            raise
        except Exception:
            logger.exception("Error processing message for session {}", msg.session_key)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Sorry, I encountered an error.",
            )

    async def process_message(
        self,
        msg: InboundMessage,
        runtime: TenantRuntime,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        tenant_key = msg.effective_tenant_key

        if msg.channel == "system":
            channel = msg.metadata.get("origin_channel") or "cli"
            chat_id = msg.metadata.get("origin_chat_id") or msg.chat_id
            logger.info("Processing system message from {}", msg.sender_id)
            key = msg.metadata.get("target_session_key") or msg.session_key
            session = runtime.sessions.get_or_create(key, tenant_key=tenant_key)
            await runtime.memory_consolidator.maybe_consolidate_by_tokens(session)
            self.set_tool_context(
                runtime,
                channel,
                chat_id,
                key,
                tenant_key,
                msg.metadata.get("message_id"),
                msg.metadata,
            )
            history = self._get_history_for_turn(
                session,
                msg.metadata,
                runtime.memory_consolidator.select_history_for_prompt,
            )
            messages = runtime.context.build_messages(
                history=history,
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
                extra_system_sections=self._get_extra_system_sections(msg.metadata),
                session_metadata=session.metadata,
            )
            deferred_actions: list[DeferredToolAction] = []
            final_content, _, all_msgs = await self.run_agent_loop(
                messages,
                runtime.context,
                deferred_actions=deferred_actions,
                stream_text=bool(msg.metadata.get("_stream_text")),
            )
            self.save_turn(session, all_msgs, 1 + len(history))
            self._apply_session_metadata(
                session,
                session_key=key,
                channel=channel,
                chat_id=chat_id,
                metadata=msg.metadata,
            )
            runtime.sessions.save(session, tenant_key=tenant_key)
            await runtime.memory_consolidator.maybe_consolidate_by_tokens(session)
            schedule_deferred_actions(
                self._postprocess_manager,
                runtime=runtime,
                channel=channel,
                chat_id=chat_id,
                session_key=key,
                tenant_key=tenant_key,
                message_id=msg.metadata.get("message_id"),
                actions=deferred_actions,
                origin_user_message=msg.content,
                assistant_reply=final_content or "",
                debug_trace=bool(msg.metadata.get("_debug_trace")),
            )
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=final_content or "Background task completed.",
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = msg.session_key
        session = runtime.sessions.get_or_create(key, tenant_key=tenant_key)

        cmd = msg.content.strip().lower()
        if cmd == "/new":
            try:
                if not await runtime.memory_consolidator.archive_unconsolidated(session):
                    return OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="Memory archival failed, session not cleared. Please try again.",
                    )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )

            session.clear()
            runtime.sessions.save(session, tenant_key=tenant_key)
            runtime.sessions.invalidate(session.key, tenant_key=tenant_key)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="New session started.",
            )

        if cmd == "/help":
            lines = [
                "🐈 nanobot commands:",
                "/new — Start a new conversation",
                "/stop — Stop the current task",
                "/restart — Restart the bot",
                "/help — Show available commands",
            ]
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="\n".join(lines),
            )

        await runtime.memory_consolidator.maybe_consolidate_by_tokens(session)
        self.set_tool_context(
            runtime,
            msg.channel,
            msg.chat_id,
            key,
            tenant_key,
            msg.metadata.get("message_id"),
            msg.metadata,
        )
        if message_tool := self._tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = self._get_history_for_turn(
            session,
            msg.metadata,
            runtime.memory_consolidator.select_history_for_prompt,
        )
        initial_messages = runtime.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            extra_system_sections=self._get_extra_system_sections(msg.metadata),
            session_metadata=session.metadata,
        )
        deferred_actions: list[DeferredToolAction] = []
        excluded_tool_names = self.excluded_tool_names_for_lane(msg.lane)

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self._bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=meta,
            ))

        final_content, _, all_msgs = await self.run_agent_loop(
            initial_messages,
            runtime.context,
            on_progress=on_progress or _bus_progress,
            deferred_actions=deferred_actions,
            stream_text=bool(msg.metadata.get("_stream_text")),
            excluded_tool_names=excluded_tool_names,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self.save_turn(session, all_msgs, 1 + len(history))
        self._apply_session_metadata(
            session,
            session_key=key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            metadata=msg.metadata,
        )
        runtime.sessions.save(session, tenant_key=tenant_key)
        await runtime.memory_consolidator.maybe_consolidate_by_tokens(session)
        schedule_deferred_actions(
            self._postprocess_manager,
            runtime=runtime,
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key=key,
            tenant_key=tenant_key,
            message_id=msg.metadata.get("message_id"),
            actions=deferred_actions,
            origin_user_message=msg.content,
            assistant_reply=final_content,
            debug_trace=bool(msg.metadata.get("_debug_trace")),
        )
        schedule_structured_memory(
            self._structured_memory_manager,
            runtime=runtime,
            channel=msg.channel,
            chat_id=msg.chat_id,
            session_key=key,
            origin_user_message=msg.content,
            assistant_reply=final_content,
            recent_messages=session.messages[-6:],
            debug_trace=bool(msg.metadata.get("_debug_trace")),
            has_explicit_memory_write=self._has_explicit_memory_write(deferred_actions),
        )

        if (mt := self._tools.get("message")) and isinstance(mt, MessageTool) and mt.sent_in_turn():
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},
        )

    def save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        save_turn_messages(
            session,
            messages,
            skip=skip,
            tool_result_max_chars=self._tool_result_max_chars,
        )
