"""Shared runtime guard for tool execution."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from simpleclaw.agent.postprocess import DeferredToolAction
from simpleclaw.agent.tools.base import Tool
from simpleclaw.agent.tools.registry import ToolRegistry
from simpleclaw.agent.tools.web import WebFetchTool, WebSearchTool, _validate_url

DeferredActionPreparer = Callable[[str, dict[str, Any]], DeferredToolAction | None]

@dataclass(slots=True)
class ToolExecutionContext:
    """Structured input for one guarded tool execution."""

    tool_name: str
    params: dict[str, Any]
    deferred_actions: list[DeferredToolAction] | None = None
    iteration: int | None = None
    turn_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolExecutionOutcome:
    """Structured runtime result for one tool execution."""

    tool_name: str
    ok: bool
    action: str
    content: str
    status: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    should_continue_llm: bool = True


GuardHandler = Callable[[ToolExecutionContext], Awaitable[ToolExecutionOutcome]]


class ToolExecutionGuard:
    """Execute tools through one thin, extensible runtime guard layer."""

    _ERROR_HINT = "\n\n[Analyze the error above and try a different approach.]"
    _WEB_SEARCH_TTL_SECONDS = 15.0
    _WEB_FETCH_TTL_SECONDS = 45.0
    _WEB_RETRY_DELAYS_SECONDS = (0.2, 0.5)
    _TRANSIENT_ERROR_MARKERS = (
        "timeout",
        "timed out",
        "temporarily unavailable",
        "connection reset",
        "connect error",
        "connection error",
        "network error",
        "proxy error",
        "server disconnected",
        "read error",
        "pool timeout",
        "transport error",
        "too many requests",
        "rate limit",
        "429",
        "502",
        "503",
        "504",
    )

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        prepare_deferred_action: DeferredActionPreparer | None = None,
    ) -> None:
        self._tools = tools
        self._prepare_deferred_action = prepare_deferred_action
        self._handlers: dict[str, GuardHandler] = {}
        self._inflight: dict[str, asyncio.Task[ToolExecutionOutcome]] = {}
        self._recent_results: dict[str, tuple[float, ToolExecutionOutcome]] = {}
        self._cache_lock = asyncio.Lock()
        self.register_handler("web_search", self._execute_web_search)
        self.register_handler("web_fetch", self._execute_web_fetch)

    def register_handler(self, tool_name: str, handler: GuardHandler) -> None:
        """Register one guarded execution handler for a tool."""
        normalized = (tool_name or "").strip()
        if not normalized:
            raise ValueError("tool_name must not be empty")
        self._handlers[normalized] = handler

    @staticmethod
    def _encode_payload(payload: dict[str, Any]) -> str:
        """Serialize one structured tool payload for the model."""
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def _build_outcome(
        cls,
        *,
        tool_name: str,
        ok: bool,
        action: str,
        content: str | None = None,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        should_continue_llm: bool = True,
    ) -> ToolExecutionOutcome:
        """Create one structured execution outcome."""
        resolved_content = content
        if resolved_content is None and result is not None:
            resolved_content = cls._encode_payload(result)
        return ToolExecutionOutcome(
            tool_name=tool_name,
            ok=ok,
            action=action,
            content=resolved_content or "",
            status=status,
            result=result,
            error=error,
            should_continue_llm=should_continue_llm,
        )

    @staticmethod
    def _stringify_error(exc: Exception) -> str:
        """Convert one exception into a compact tool-execution error."""
        return str(exc).strip() or exc.__class__.__name__

    def _resolve_tool(self, tool_name: str) -> Tool | None:
        """Resolve one registered tool by name."""
        return self._tools.get(tool_name)

    @staticmethod
    def _canonicalize_params(params: dict[str, Any]) -> str:
        """Serialize params into a stable cache-key fragment."""
        return json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _copy_outcome(
        outcome: ToolExecutionOutcome,
        *,
        action: str | None = None,
        status: str | None = None,
    ) -> ToolExecutionOutcome:
        """Clone one execution outcome with updated runtime semantics."""
        return ToolExecutionOutcome(
            tool_name=outcome.tool_name,
            ok=outcome.ok,
            action=action or outcome.action,
            content=outcome.content,
            status=status or outcome.status,
            result=dict(outcome.result) if outcome.result is not None else None,
            error=outcome.error,
            should_continue_llm=outcome.should_continue_llm,
        )

    def _build_scope_key(self, context: ToolExecutionContext) -> str:
        """Build one session-scoped identity prefix for dedupe/idempotence."""
        metadata = context.turn_metadata
        tenant_key = str(metadata.get("tenant_key") or "")
        session_key = str(metadata.get("session_key") or "")
        lane = str(metadata.get("lane") or "")
        return "|".join((tenant_key, session_key, lane))

    def _build_execution_key(self, context: ToolExecutionContext) -> str:
        """Build one deterministic execution key for a tool call."""
        return "|".join((
            self._build_scope_key(context),
            context.tool_name,
            self._canonicalize_params(context.params),
        ))

    async def _cleanup_recent_results(self) -> None:
        """Drop expired recent-result entries."""
        now = time.monotonic()
        expired_keys = [
            key
            for key, (expires_at, _) in self._recent_results.items()
            if expires_at <= now
        ]
        for key in expired_keys:
            self._recent_results.pop(key, None)

    @classmethod
    def _with_error_hint(cls, text: str) -> str:
        """Append the standard retry hint once for model-facing errors."""
        return text if text.endswith(cls._ERROR_HINT) else text + cls._ERROR_HINT

    def _invalid_outcome(
        self,
        *,
        tool_name: str,
        error: str,
        status: str = "invalid",
        result: dict[str, Any] | None = None,
    ) -> ToolExecutionOutcome:
        """Build one invalid-parameters outcome."""
        payload = result or {
            "ok": False,
            "action": "invalid",
            "status": status,
            "error": error,
        }
        return self._build_outcome(
            tool_name=tool_name,
            ok=False,
            action="invalid",
            status=status,
            result=payload,
            error=error,
        )

    def _error_outcome(
        self,
        *,
        tool_name: str,
        error: str,
        status: str = "error",
        result: dict[str, Any] | None = None,
    ) -> ToolExecutionOutcome:
        """Build one execution-error outcome."""
        payload = result or {
            "ok": False,
            "action": "error",
            "status": status,
            "error": error,
        }
        return self._build_outcome(
            tool_name=tool_name,
            ok=False,
            action="error",
            status=status,
            content=self._with_error_hint(error),
            result=payload,
            error=error,
        )

    def _is_retryable_web_outcome(self, outcome: ToolExecutionOutcome) -> bool:
        """Return whether one web-tool failure looks transient."""
        if outcome.ok:
            return False
        if outcome.action == "invalid":
            return False

        parts = [
            outcome.error or "",
            outcome.content or "",
            json.dumps(outcome.result or {}, ensure_ascii=False),
        ]
        haystack = "\n".join(parts).lower()
        return any(marker in haystack for marker in self._TRANSIENT_ERROR_MARKERS)

    async def _execute_with_retry(
        self,
        context: ToolExecutionContext,
        runner: Callable[[], Awaitable[ToolExecutionOutcome]],
    ) -> ToolExecutionOutcome:
        """Retry one tool a small number of times on transient web failures."""
        attempts = len(self._WEB_RETRY_DELAYS_SECONDS) + 1
        last_outcome: ToolExecutionOutcome | None = None
        for attempt in range(1, attempts + 1):
            outcome = await runner()
            last_outcome = outcome
            if outcome.ok or not self._is_retryable_web_outcome(outcome):
                if attempt > 1:
                    logger.info(
                        "{} guard finished after {} attempt(s) with status={}",
                        context.tool_name,
                        attempt,
                        outcome.status,
                    )
                return outcome

            if attempt >= attempts:
                break

            delay = self._WEB_RETRY_DELAYS_SECONDS[attempt - 1]
            logger.info(
                "{} guard retrying transient failure on attempt {} after {}s",
                context.tool_name,
                attempt,
                delay,
            )
            await asyncio.sleep(delay)

        return last_outcome or self._error_outcome(
            tool_name=context.tool_name,
            error=f"Error executing {context.tool_name}: retry pipeline returned no outcome",
            status="retry_failed",
        )

    async def _execute_with_dedupe(
        self,
        context: ToolExecutionContext,
        *,
        ttl_seconds: float,
        runner: Callable[[], Awaitable[ToolExecutionOutcome]],
    ) -> ToolExecutionOutcome:
        """Reuse in-flight and recent session-scoped tool executions."""
        key = self._build_execution_key(context)
        async with self._cache_lock:
            await self._cleanup_recent_results()
            cached = self._recent_results.get(key)
            if cached is not None:
                _, cached_outcome = cached
                logger.info("{} guard noop reused recent result", context.tool_name)
                return self._copy_outcome(
                    cached_outcome,
                    action="noop",
                    status="recent_result_reused",
                )

            task = self._inflight.get(key)
            owner = task is None
            if owner:
                task = asyncio.create_task(runner())
                self._inflight[key] = task
            assert task is not None

        try:
            outcome = await task
        finally:
            if owner:
                async with self._cache_lock:
                    current_task = self._inflight.get(key)
                    if current_task is task:
                        self._inflight.pop(key, None)

        if owner:
            if outcome.ok:
                async with self._cache_lock:
                    self._recent_results[key] = (time.monotonic() + ttl_seconds, outcome)
            return outcome

        logger.info("{} guard deduped to in-flight execution", context.tool_name)
        return self._copy_outcome(
            outcome,
            action="deduped",
            status="inflight_reused",
        )

    async def _execute_default_pipeline(self, context: ToolExecutionContext) -> ToolExecutionOutcome:
        """Run the shared prepare/validate/execute/finalize pipeline."""
        tool = self._resolve_tool(context.tool_name)
        if tool is None:
            available = ", ".join(self._tools.tool_names)
            error = f"Error: Tool '{context.tool_name}' not found. Available: {available}"
            return self._error_outcome(
                tool_name=context.tool_name,
                status="tool_not_found",
                error=error,
            )

        try:
            normalized_params = tool.cast_params(dict(context.params))
        except Exception as exc:
            error = f"Error: Failed to normalize parameters for '{context.tool_name}': {self._stringify_error(exc)}"
            logger.warning(error)
            return self._error_outcome(
                tool_name=context.tool_name,
                status="cast_failed",
                error=error,
            )

        try:
            errors = tool.validate_params(normalized_params)
        except Exception as exc:
            error = f"Error: Validator failed for '{context.tool_name}': {self._stringify_error(exc)}"
            logger.warning(error)
            return self._error_outcome(
                tool_name=context.tool_name,
                status="validator_failed",
                error=error,
            )

        if errors:
            error = f"Error: Invalid parameters for tool '{context.tool_name}': " + "; ".join(errors)
            logger.info("{} default guard rejected params: {}", context.tool_name, "; ".join(errors))
            return self._invalid_outcome(
                tool_name=context.tool_name,
                error=error,
                result={
                    "ok": False,
                    "action": "invalid",
                    "status": "invalid",
                    "error": error,
                },
            )

        try:
            result = await tool.execute(**normalized_params)
        except Exception as exc:
            error = f"Error executing {context.tool_name}: {self._stringify_error(exc)}"
            logger.warning("{} default guard execution failed: {}", context.tool_name, error)
            return self._error_outcome(
                tool_name=context.tool_name,
                status="execute_failed",
                error=error,
            )

        if isinstance(result, str) and result.startswith("Error"):
            return self._error_outcome(
                tool_name=context.tool_name,
                status="tool_error_result",
                error=result.removesuffix(self._ERROR_HINT),
                result={
                    "ok": False,
                    "action": "error",
                    "status": "tool_error_result",
                    "error": result.removesuffix(self._ERROR_HINT),
                },
            )

        return self._build_outcome(
            tool_name=context.tool_name,
            ok=True,
            action="executed",
            content=result,
            status="executed",
        )

    async def execute(self, context: ToolExecutionContext) -> ToolExecutionOutcome:
        """Execute one tool call through deferred guards and optional handlers."""
        if self._prepare_deferred_action is not None:
            deferred = self._prepare_deferred_action(context.tool_name, dict(context.params))
            if deferred is not None:
                if context.deferred_actions is not None:
                    context.deferred_actions.append(deferred)
                return self._build_outcome(
                    tool_name=context.tool_name,
                    ok=True,
                    action="deferred",
                    content=deferred.synthetic_result,
                    status="deferred",
                )

        handler = self._handlers.get(context.tool_name)
        if handler is not None:
            return await handler(context)
        return await self._execute_default_pipeline(context)

    async def _execute_web_search(self, context: ToolExecutionContext) -> ToolExecutionOutcome:
        """Run deterministic validation and execution for web_search."""
        tool = self._resolve_tool(context.tool_name)
        if not isinstance(tool, WebSearchTool):
            return await self._execute_default_pipeline(context)

        query = str(context.params.get("query") or "").strip()
        if not query:
            return self._invalid_outcome(
                tool_name=context.tool_name,
                error="Error: Invalid parameters for tool 'web_search': missing required query",
            )

        outcome = await self._execute_with_dedupe(
            context,
            ttl_seconds=self._WEB_SEARCH_TTL_SECONDS,
            runner=lambda: self._execute_with_retry(
                context,
                lambda: self._execute_default_pipeline(context),
            ),
        )
        if outcome.ok:
            logger.info(
                "web_search guard executed query={} iteration={}",
                query,
                context.iteration,
            )
        return outcome

    async def _execute_web_fetch(self, context: ToolExecutionContext) -> ToolExecutionOutcome:
        """Run deterministic validation and lightweight URL precheck for web_fetch."""
        tool = self._resolve_tool(context.tool_name)
        if not isinstance(tool, WebFetchTool):
            return await self._execute_default_pipeline(context)

        raw_url = context.params.get("url")
        url = str(raw_url or "").strip()
        if not url:
            return self._invalid_outcome(
                tool_name=context.tool_name,
                error="Error: Invalid parameters for tool 'web_fetch': missing required url",
            )

        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            error = f"Error: Invalid parameters for tool 'web_fetch': URL validation failed: {error_msg}"
            logger.info("web_fetch guard rejected url: {}", error_msg)
            return self._invalid_outcome(
                tool_name=context.tool_name,
                error=error,
                result={
                    "ok": False,
                    "action": "invalid",
                    "status": "invalid_url",
                    "error": error,
                    "url": url,
                },
            )

        outcome = await self._execute_with_dedupe(
            context,
            ttl_seconds=self._WEB_FETCH_TTL_SECONDS,
            runner=lambda: self._execute_with_retry(
                context,
                lambda: self._execute_default_pipeline(context),
            ),
        )
        if outcome.ok:
            logger.info("web_fetch guard executed url={} iteration={}", url, context.iteration)
        return outcome
