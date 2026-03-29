"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger

# Keep these imports in this module for backwards compatibility in tests and patches.
from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryConsolidator, MemoryStore
from nanobot.agent.postprocess import PostprocessManager, PostprocessRunner
from nanobot.agent.runtime import TenantRuntime, TenantRuntimeManager
from nanobot.agent.scheduler import SessionEnvelope, SessionScheduler
from nanobot.agent.structured_memory import StructuredMemoryManager
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import (
    CronAddIntervalTool,
    CronAddOnceTool,
    CronListTool,
    CronRemoveTool,
    CronTool,
)
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.agent.turn_processor import TurnProcessor
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.heartbeat.service import _HEARTBEAT_TOOL
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager
from nanobot.session.primary_session import PrimarySessionStore
from nanobot.runtime.leases import LeaseRepository
from nanobot.tenant.state import TenantStateRepository

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, CronExecutionPolicy, ExecToolConfig, WebSearchConfig
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Routes them into session mailboxes
    3. Builds context with history, memory, skills
    4. Calls the LLM and executes tools
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 16_000

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        context_window_tokens: int = 65_536,
        web_search_config: WebSearchConfig | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        cron_deliver_default: bool = True,
        cron_execution_policy_default: CronExecutionPolicy = "isolated-per-job",
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        create_session_manager: Callable[[Path, str], SessionManager] | None = None,
        create_context_builder: Callable[[Path, str], ContextBuilder] | None = None,
        create_memory_consolidator: Callable[[Path, str, SessionManager, ContextBuilder], MemoryConsolidator] | None = None,
        provision_tenant: Callable[[str], None] | None = None,
        ensure_tenant_workspace: bool = True,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        tenant_state_repo: TenantStateRepository | None = None,
        lease_repo: LeaseRepository | None = None,
        document_store: object | None = None,
        memory_store: object | None = None,
        lane_limits: dict[str, int] | None = None,
        lane_backlog_caps: dict[str, int] | None = None,
        lane_tenant_limits: dict[str, int] | None = None,
        postprocess_enabled: bool = True,
        postprocess_max_concurrency: int = 2,
        defer_cron_writes: bool = True,
        defer_heartbeat_writes: bool = True,
        defer_memory_writes: bool = True,
        structured_memory_enabled: bool = True,
        structured_memory_recent_messages: int = 6,
        session_ownership_ttl_s: int = 0,
    ):
        from nanobot.config.schema import ExecToolConfig, WebSearchConfig

        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.context_window_tokens = context_window_tokens
        self.web_search_config = web_search_config or WebSearchConfig()
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.cron_deliver_default = cron_deliver_default
        self.cron_execution_policy_default = cron_execution_policy_default
        self.restrict_to_workspace = restrict_to_workspace

        self.tools = ToolRegistry()
        self._session_manager_override = session_manager
        self._create_session_manager = create_session_manager
        self._create_context_builder = create_context_builder
        self._create_memory_consolidator = create_memory_consolidator
        self._provision_tenant = provision_tenant
        self._ensure_tenant_workspace = ensure_tenant_workspace
        self._tenant_state_repo = tenant_state_repo
        self._lease_repo = lease_repo
        self._document_store = document_store
        self._memory_store = memory_store
        self._primary_sessions = PrimarySessionStore(repository=tenant_state_repo)
        self._lane_limits = lane_limits or {}
        self._lane_backlog_caps = lane_backlog_caps or {}
        self._lane_tenant_limits = lane_tenant_limits or {}
        self._session_ownership_ttl_s = max(0, session_ownership_ttl_s)
        self._postprocess_manager = PostprocessManager(
            self.tools,
            self._set_tool_context,
            enabled=postprocess_enabled,
            max_concurrency=postprocess_max_concurrency,
        )
        self._structured_memory_manager = StructuredMemoryManager(
            provider=self.provider,
            model=self.model,
            enabled=postprocess_enabled and structured_memory_enabled,
            max_concurrency=1,
            recent_message_limit=structured_memory_recent_messages,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._register_default_tools()

        self._runtime_manager = TenantRuntimeManager(
            workspace=self.workspace,
            provider=self.provider,
            model=self.model,
            context_window_tokens=self.context_window_tokens,
            tools_get_definitions=self.tools.get_definitions,
            create_subagent_manager=self._create_subagent_manager,
            create_context_builder=(
                self._create_context_builder
                or (lambda workspace, tenant: ContextBuilder(
                    workspace,
                    tenant_key=tenant,
                    document_store=self._document_store,
                    memory_store=self._memory_store,
                    tenant_state_repo=self._tenant_state_repo,
                    context_window_tokens=self.context_window_tokens,
                ))
            ),
            create_session_manager=(
                self._create_session_manager
                or (lambda workspace, tenant: (
                    self._session_manager_override
                    if tenant == "__default__" and self._session_manager_override is not None
                    else SessionManager(workspace, tenant_key=tenant)
                ))
            ),
            create_memory_consolidator=(
                self._create_memory_consolidator
                or (lambda workspace, tenant, sessions, context: MemoryConsolidator(
                    workspace=workspace,
                    provider=self.provider,
                    model=self.model,
                    sessions=sessions,
                    context_window_tokens=self.context_window_tokens,
                    build_messages=context.build_messages,
                    get_tool_definitions=self.tools.get_definitions,
                    memory_store=MemoryStore(workspace, tenant_key=tenant, repository=self._memory_store),
                ))
            ),
            provision_tenant=self._provision_tenant,
            ensure_tenant_workspace=self._ensure_tenant_workspace,
        )
        self._turn_processor = TurnProcessor(
            provider=self.provider,
            model=self.model,
            max_iterations=self.max_iterations,
            tools=self.tools,
            bus=self.bus,
            restrict_to_workspace=self.restrict_to_workspace,
            tool_result_max_chars=self._TOOL_RESULT_MAX_CHARS,
            default_workspace=self.workspace,
            postprocess_manager=self._postprocess_manager,
            structured_memory_manager=self._structured_memory_manager,
            defer_cron_writes=defer_cron_writes,
            defer_heartbeat_writes=defer_heartbeat_writes,
            defer_memory_writes=defer_memory_writes,
        )
        self._scheduler = SessionScheduler(
            process_message=self._dispatch_response,
            publish_outbound=self.bus.publish_outbound,
            idle_timeout_s=30.0,
            on_task_start=self._mark_session_active,
            on_task_end=self._mark_session_idle,
            lane_limits=self._lane_limits,
            lane_backlog_caps=self._lane_backlog_caps,
            lane_tenant_limits=self._lane_tenant_limits,
        )

        # Keep these aliases for compatibility with existing tests and debugging.
        self._tenant_runtimes = self._runtime_manager.runtimes
        self._active_tasks = self._scheduler.active_tasks
        self._session_mailboxes = self._scheduler.session_mailboxes
        self._session_workers = self._scheduler.session_workers

    @property
    def context(self) -> ContextBuilder:
        """Default-tenant context builder."""
        return self._get_runtime(None).context

    @property
    def sessions(self) -> SessionManager:
        """Default-tenant session manager."""
        return self._get_runtime(None).sessions

    @property
    def memory_consolidator(self) -> MemoryConsolidator:
        """Default-tenant memory consolidator."""
        return self._get_runtime(None).memory_consolidator

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(
                cls(
                    workspace=self.workspace,
                    allowed_dir=allowed_dir,
                    document_store=self._document_store,
                    memory_store=self._memory_store,
                    tenant_state_repo=self._tenant_state_repo,
                )
            )
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        self.tools.register(WebSearchTool(config=self.web_search_config, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self._create_subagent_manager(self.workspace)))
        if self.cron_service:
            cron_tool = CronTool(
                self.cron_service,
                default_deliver=self.cron_deliver_default,
                default_execution_policy=self.cron_execution_policy_default,
            )
            self.tools.register(cron_tool, expose_to_llm=False)
            self.tools.register(CronAddOnceTool(cron_tool))
            self.tools.register(CronAddIntervalTool(cron_tool))
            self.tools.register(CronListTool(cron_tool))
            self.tools.register(CronRemoveTool(cron_tool))

    def _create_subagent_manager(self, workspace: Path) -> SubagentManager:
        """Create a subagent manager scoped to one workspace."""
        return SubagentManager(
            provider=self.provider,
            workspace=workspace,
            bus=self.bus,
            model=self.model,
            web_search_config=self.web_search_config,
            web_proxy=self.web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=self.restrict_to_workspace,
            max_concurrency=self._lane_limits.get("subagent", 4),
            document_store=self._document_store,
            memory_store=self._memory_store,
            tenant_state_repo=self._tenant_state_repo,
        )

    def _get_runtime(self, tenant_key: str | None) -> TenantRuntime:
        """Return a tenant-scoped runtime, creating it lazily."""
        return self._runtime_manager.get_runtime(tenant_key)

    def set_postprocess_runner(self, runner: PostprocessRunner | None) -> None:
        """Attach a dedicated postprocess runner after construction."""
        self._postprocess_manager.set_runner(runner)

    def set_structured_memory_runner(self, runner) -> None:
        """Attach a dedicated structured-memory runner after construction."""
        self._structured_memory_manager.set_runner(runner)

    def disable_deferred_postprocess(self) -> None:
        """Disable deferred-write interception in this loop."""
        self._turn_processor.disable_deferred_postprocess()

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except BaseException as exc:
            logger.error("Failed to connect MCP servers (will retry next message): {}", exc)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(
        self,
        runtime: TenantRuntime,
        channel: str,
        chat_id: str,
        session_key: str,
        tenant_key: str,
        message_id: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        self._turn_processor.set_tool_context(
            runtime,
            channel,
            chat_id,
            session_key,
            tenant_key,
            message_id,
        )

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        return TurnProcessor.strip_think(text)

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        return TurnProcessor.tool_hint(tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        context_builder: ContextBuilder | None = None,
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop."""
        return await self._turn_processor.run_agent_loop(
            initial_messages,
            context_builder=context_builder,
            on_progress=on_progress,
        )

    async def run(self) -> None:
        """Run the agent loop and route inbound messages into session mailboxes."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            cmd = msg.content.strip().lower()
            if cmd == "/stop":
                await self._handle_stop(msg)
            elif cmd == "/restart":
                await self._handle_restart(msg)
            else:
                self._maybe_track_primary_session(msg)
                await self._submit_envelope(SessionEnvelope(msg=msg))

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        runtime = self._get_runtime(msg.effective_tenant_key)
        cancelled, dropped = await self._scheduler.cancel_session(msg.routing_key)
        sub_cancelled = await runtime.subagents.cancel_by_session(
            msg.session_key,
            msg.effective_tenant_key,
        )
        total = cancelled + sub_cancelled + dropped
        content = f"Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
        ))

    async def _handle_restart(self, msg: InboundMessage) -> None:
        """Restart the process in-place via os.execv."""
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="Restarting...",
        ))

        async def _do_restart():
            await asyncio.sleep(1)
            os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

        asyncio.create_task(_do_restart())

    async def _dispatch_response(
        self,
        msg: InboundMessage,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process one queued message without publishing it directly."""
        runtime = self._get_runtime(msg.effective_tenant_key)
        return await self._turn_processor.dispatch(msg, runtime, on_progress=on_progress)

    async def _dispatch(
        self,
        msg: InboundMessage,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Route one message through the scheduler and publish the final reply."""
        future: asyncio.Future[OutboundMessage | None] = asyncio.get_running_loop().create_future()
        response = await self._submit_envelope(
            SessionEnvelope(msg=msg, future=future, on_progress=on_progress)
        )
        if response is not None:
            await self.bus.publish_outbound(response)
        elif msg.channel == "cli":
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="",
                metadata=msg.metadata or {},
            ))
        return response

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        self._scheduler.stop()
        logger.info("Agent loop stopping")

    def get_session_load(self, session_key: str, tenant_key: str | None = None) -> dict[str, int | bool]:
        """Expose current scheduler state for one tenant/session."""
        effective_tenant = tenant_key or "__default__"
        return self._scheduler.get_session_load(f"{effective_tenant}:{session_key}")

    async def _mark_session_active(self, msg: InboundMessage) -> None:
        """Publish a cross-process busy lease for one session while it is executing."""
        if self._lease_repo is None:
            return
        self._lease_repo.acquire("session-busy", msg.routing_key, ttl_s=60 * 60)

    async def _mark_session_idle(self, msg: InboundMessage) -> None:
        """Clear the cross-process busy lease for one session after execution."""
        if self._lease_repo is None:
            return
        load = self._scheduler.get_session_load(msg.routing_key)
        if load["active_count"] or load["queued_count"]:
            return
        self._lease_repo.release("session-busy", msg.routing_key)

    def _maybe_track_primary_session(self, msg: InboundMessage) -> None:
        """Refresh the tenant -> primary session route for real user turns."""
        if msg.metadata.get("_track_primary_session") is False:
            return
        if msg.channel in {"cli", "system", "cron", "heartbeat"}:
            return
        if self._tenant_state_repo is not None:
            self._tenant_state_repo.touch_interaction(
                tenant_key=msg.effective_tenant_key,
                session_key=msg.session_key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                activity_at_ms=int(msg.timestamp.timestamp() * 1000),
            )
            return
        self._primary_sessions.save(
            tenant_key=msg.effective_tenant_key,
            session_key=msg.session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )

    async def _submit_envelope(self, envelope: SessionEnvelope) -> OutboundMessage | None:
        """Route a message into its session mailbox and optionally await completion."""
        return await self._scheduler.submit(envelope)

    async def _claim_session_ownership(self, routing_key: str) -> bool:
        if self._lease_repo is None or self._session_ownership_ttl_s <= 0:
            return True
        return await asyncio.to_thread(
            self._lease_repo.acquire,
            "session-owner",
            routing_key,
            ttl_s=self._session_ownership_ttl_s,
        )

    async def _release_session_ownership(self, routing_key: str) -> None:
        if self._lease_repo is None or self._session_ownership_ttl_s <= 0:
            return
        await asyncio.to_thread(self._lease_repo.release, "session-owner", routing_key)

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        if session_key and session_key != msg.session_key:
            msg = InboundMessage(
                channel=msg.channel,
                sender_id=msg.sender_id,
                chat_id=msg.chat_id,
                content=msg.content,
                tenant_key=msg.tenant_key,
                timestamp=msg.timestamp,
                media=list(msg.media),
                metadata=dict(msg.metadata),
                session_key_override=session_key,
            )
        runtime = self._get_runtime(msg.effective_tenant_key)
        return await self._turn_processor.process_message(
            msg,
            runtime,
            on_progress=on_progress,
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        self._turn_processor.save_turn(session, messages, skip)

    def get_last_llm_debug(self) -> list[dict[str, Any]]:
        """Return provider/cache debug info for the most recent turn."""
        return self._turn_processor.get_last_llm_debug()

    def get_llm_tool_definitions(self, *, lane: str = "main") -> list[dict[str, Any]]:
        """Return the tool schemas exposed to the model for one lane."""
        return self._turn_processor.get_llm_tool_definitions(lane=lane)

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        tenant_key: str | None = None,
        media: list[str] | None = None,
        message_id: str | None = None,
        outbound_session_key: str | None = None,
        session_type: str | None = None,
        origin_session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        extra_system_sections: list[str] | None = None,
        track_primary_session: bool = False,
        lane: str = "main",
        stream_text: bool = False,
        debug_trace: bool = False,
        suppress_history: bool = False,
    ) -> str:
        """Process a message directly using the same mailbox model as bus turns."""
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content,
            tenant_key=tenant_key,
            media=list(media or []),
            metadata={
                "_extra_system_sections": extra_system_sections or [],
                "_track_primary_session": track_primary_session,
                "_lane": lane,
                "_stream_text": stream_text,
                "_debug_trace": debug_trace,
                "_session_type": session_type,
                "_origin_session_key": origin_session_key,
                "_suppress_history": suppress_history,
                "message_id": message_id,
                "_outbound_session_key": outbound_session_key,
            },
            session_key_override=session_key,
        )
        self._maybe_track_primary_session(msg)
        if not await self._claim_session_ownership(msg.routing_key):
            raise RuntimeError(f"Session is already owned by another worker: {msg.routing_key}")
        future: asyncio.Future[OutboundMessage | None] = asyncio.get_running_loop().create_future()
        try:
            response = await self._submit_envelope(
                SessionEnvelope(msg=msg, future=future, on_progress=on_progress)
            )
            return response.content if response else ""
        finally:
            await self._release_session_ownership(msg.routing_key)
    ##心跳机制的决策入口
    async def decide_heartbeat(
        self,
        heartbeat_content: str,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
        tenant_key: str | None = None,
    ) -> tuple[str, str]:
        """Decide whether a heartbeat should skip or run using main session context."""
        await self._connect_mcp()
        runtime = self._get_runtime(tenant_key)
        session = runtime.sessions.get_or_create(session_key, tenant_key=tenant_key)
        await runtime.memory_consolidator.maybe_consolidate_by_tokens(session)

        messages = runtime.context.build_messages(
            history=[],
            current_message=(
                "[Heartbeat Decision]\n"
                "This is a periodic heartbeat check for the current active session.\n"
                "Use only the injected heartbeat context to decide whether there are lightweight active tasks worth running now.\n"
                "If nothing needs to be done, choose skip. If work should run now, choose run and summarize the tasks.\n\n"
                "Do not infer new tasks from unrelated chat history, cron jobs, or other session context.\n"
                "Return skip when there is nothing worth doing right now."
            ),
            channel=channel,
            chat_id=chat_id,
            extra_system_sections=[
                "# Heartbeat Context\n"
                "The following context was injected by the periodic heartbeat.\n\n"
                "## HEARTBEAT.md\n"
                f"{heartbeat_content}"
            ],
        )
        response = await self.provider.chat_with_retry(
            messages=messages,
            tools=_HEARTBEAT_TOOL,
            model=self.model,
        )
        if not response.has_tool_calls:
            return "skip", ""
        args = response.tool_calls[0].arguments
        return args.get("action", "skip"), args.get("tasks", "")
