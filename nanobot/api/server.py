"""Minimal HTTP API server for driving the chat-api runtime role."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from nanobot.bus.events import OutboundMessage
from nanobot.runtime.bootstrap import RuntimeServices, build_runtime_services


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@dataclass(slots=True)
class ApiSseEvent:
    event: str
    payload: dict[str, Any]


class ApiEventHub:
    """In-process fanout hub for API-facing outbound events."""

    def __init__(self, *, replay_limit: int = 32) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[ApiSseEvent]]] = defaultdict(set)
        self._recent: dict[str, deque[ApiSseEvent]] = defaultdict(lambda: deque(maxlen=replay_limit))
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(tenant_key: str, conversation_id: str) -> str:
        return f"{tenant_key}:{conversation_id}"

    async def publish_outbound(self, message: OutboundMessage) -> None:
        if message.channel != "api":
            return
        tenant_key = message.tenant_key or "__default__"
        event = ApiSseEvent(
            event=str(message.metadata.get("_event") or "background_message"),
            payload={
                "ok": True,
                "channel": message.channel,
                "chat_id": message.chat_id,
                "content": message.content,
                "tenant_key": tenant_key,
                "session_key": message.session_key,
                "media": list(message.media),
                "metadata": dict(message.metadata or {}),
            },
        )
        key = self._key(tenant_key, message.chat_id)
        async with self._lock:
            self._recent[key].append(event)
            subscribers = list(self._subscribers.get(key, set()))
        for queue in subscribers:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    async def subscribe(
        self,
        *,
        tenant_key: str,
        conversation_id: str,
    ) -> tuple[asyncio.Queue[ApiSseEvent], list[ApiSseEvent]]:
        key = self._key(tenant_key, conversation_id)
        queue: asyncio.Queue[ApiSseEvent] = asyncio.Queue(maxsize=64)
        async with self._lock:
            self._subscribers[key].add(queue)
            replay = list(self._recent.get(key, ()))
        return queue, replay

    async def unsubscribe(
        self,
        *,
        tenant_key: str,
        conversation_id: str,
        queue: asyncio.Queue[ApiSseEvent],
    ) -> None:
        key = self._key(tenant_key, conversation_id)
        async with self._lock:
            subscribers = self._subscribers.get(key)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(key, None)


def _encode_sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _content_to_debug_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            if item.get("type") == "text" and item.get("text"):
                parts.append(str(item["text"]))
            elif item.get("type") == "image_url":
                parts.append("[image omitted]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def _build_turn_timing_payload(
    *,
    request_started_at: float,
    prompt_snapshot_sent_at: float | None = None,
    llm_request_started_at: float | None = None,
    first_text_delta_at: float | None = None,
    final_response_at: float | None = None,
    llm_debug: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a compact timing breakdown for one streamed turn."""

    def _elapsed_ms(when: float | None, *, since: float) -> int | None:
        if when is None:
            return None
        return max(0, int((when - since) * 1000))

    payload: dict[str, Any] = {
        "accepted_to_prompt_snapshot_ms": _elapsed_ms(
            prompt_snapshot_sent_at,
            since=request_started_at,
        ),
        "accepted_to_first_token_ms": _elapsed_ms(
            first_text_delta_at,
            since=request_started_at,
        ),
        "accepted_to_final_ms": _elapsed_ms(
            final_response_at,
            since=request_started_at,
        ),
        "prompt_snapshot_to_first_token_ms": None,
        "llm_wait_ms": None,
        "streaming_after_first_token_ms": None,
    }

    if prompt_snapshot_sent_at is not None and first_text_delta_at is not None:
        payload["prompt_snapshot_to_first_token_ms"] = _elapsed_ms(
            first_text_delta_at,
            since=prompt_snapshot_sent_at,
        )
    if llm_request_started_at is not None and first_text_delta_at is not None:
        payload["llm_wait_ms"] = _elapsed_ms(
            first_text_delta_at,
            since=llm_request_started_at,
        )
    if first_text_delta_at is not None and final_response_at is not None:
        payload["streaming_after_first_token_ms"] = _elapsed_ms(
            final_response_at,
            since=first_text_delta_at,
        )
    if llm_debug:
        payload["llm"] = llm_debug
    return payload


def _render_prompt_snapshot(
    messages: list[dict[str, Any]],
    tool_defs: list[dict[str, Any]] | None = None,
    prompt_state: dict[str, Any] | None = None,
) -> str:
    snapshot = _build_prompt_snapshot_data(messages, tool_defs, prompt_state)
    blocks: list[str] = []
    blocks.append("## Prefix Cache View")
    blocks.append(
        "### Stable Prefix [cached via common_prefix]\n"
        + (snapshot["stable_prefix"] or "(none)")
    )
    blocks.append(
        "### Stable Tool Schemas [cached with prefix]\n"
        + (
            json.dumps(snapshot["tool_schemas"], ensure_ascii=False, indent=2)
            if snapshot["tool_schemas"]
            else "(none)"
        )
    )
    blocks.append(
        "### Dynamic Tail [recomputed every turn]\n"
        + (snapshot["dynamic_tail"] or "(none)")
    )
    blocks.append(
        "### Runtime Request Messages [recomputed every turn]\n"
        + (snapshot["request_messages_preview"] or "(none)")
    )

    blocks.append("")
    blocks.append("## Full System Prompt")
    blocks.append(snapshot["full_system_prompt"] or "(none)")

    blocks.append("")
    blocks.append("## Tool Schemas")
    if snapshot["tool_schemas"]:
        blocks.append(json.dumps(snapshot["tool_schemas"], ensure_ascii=False, indent=2))
    else:
        blocks.append("(none)")

    blocks.append("")
    blocks.append("## Prompt Observability")
    if prompt_state:
        blocks.append(json.dumps(prompt_state, ensure_ascii=False, indent=2))
    else:
        blocks.append("(none)")

    blocks.append("")
    blocks.append("## Request Messages")
    if snapshot["skip_first_system"]:
        blocks.append("(system message shown above)")
    for item in snapshot["request_messages"]:
        blocks.append(f"## Message {item['index']} [{item['role']}]")
        blocks.append(item["content"])
        if item["tool_calls"]:
            blocks.append("")
            blocks.append("### tool_calls")
            blocks.append(json.dumps(item["tool_calls"], ensure_ascii=False, indent=2))
        if item["tool_call_id"]:
            blocks.append("")
            blocks.append(f"### tool_call_id\n{item['tool_call_id']}")
    return "\n\n".join(blocks)


def _build_prompt_snapshot_data(
    messages: list[dict[str, Any]],
    tool_defs: list[dict[str, Any]] | None = None,
    prompt_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del prompt_state
    system_prompt = ""
    stable_prefix = ""
    dynamic_tail = ""
    skip_first_system = bool(messages) and str(messages[0].get("role") or "").lower() == "system"
    if skip_first_system:
        system_prompt = _content_to_debug_text(messages[0].get("content"))
        stable_prefix = _content_to_debug_text(messages[0].get("_cache_stable_prefix"))
        dynamic_tail = _content_to_debug_text(messages[0].get("_cache_dynamic_tail"))
    if not stable_prefix:
        stable_prefix = system_prompt

    if skip_first_system:
        display_messages = messages[1:]
        start_index = 2
    else:
        display_messages = messages
        start_index = 1

    request_messages: list[dict[str, Any]] = []
    preview_blocks: list[str] = []
    for index, message in enumerate(display_messages, start=start_index):
        role = str(message.get("role") or "unknown").upper()
        content = _content_to_debug_text(message.get("content"))
        tool_calls = message.get("tool_calls")
        tool_call_id = message.get("tool_call_id")
        request_messages.append(
            {
                "index": index,
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
                "tool_call_id": tool_call_id,
            }
        )
        preview_blocks.append(f"## Message {index} [{role}]")
        preview_blocks.append(content)

    return {
        "full_system_prompt": system_prompt,
        "stable_prefix": stable_prefix,
        "dynamic_tail": dynamic_tail,
        "tool_schemas": tool_defs or [],
        "request_messages": request_messages,
        "request_messages_preview": "\n\n".join(preview_blocks),
        "skip_first_system": skip_first_system,
    }


def run_api_server(
    *,
    host: str,
    port: int,
    workspace: str | None,
    config: str | None,
    verbose: bool,
    console: Any,
    logo: str,
) -> None:
    """Start the HTTP API server."""
    runtime = build_runtime_services(
        console=console,
        role="chat-api",
        config_path=config,
        workspace=workspace,
    )
    run_api_server_with_runtime(
        runtime=runtime,
        host=host,
        port=port,
        verbose=verbose,
        console=console,
        logo=logo,
    )


def run_api_server_with_runtime(
    *,
    runtime: RuntimeServices,
    host: str,
    port: int,
    verbose: bool,
    console: Any,
    logo: str,
) -> None:
    """Start the HTTP API server with a prebuilt chat-api runtime."""
    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    agent_loop = runtime.agent_loop
    event_hub = ApiEventHub()
    outbound_consumer_group = f"{runtime.config.runtime.tasks.consumer_group}:api-sse:{port}"

    def _pick_value(payload: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    def _pick_media(payload: dict[str, Any]) -> list[str]:
        values: list[str] = []
        for key in ("media", "images", "image_urls", "imageUrls"):
            raw = payload.get(key)
            if isinstance(raw, str) and raw.strip():
                values.append(raw.strip())
            elif isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str) and item.strip():
                        values.append(item.strip())
        return values

    def _pick_bool(payload: dict[str, Any], *keys: str) -> bool:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                text = value.strip().lower()
                if text in {"1", "true", "yes", "on"}:
                    return True
                if text in {"0", "false", "no", "off"}:
                    return False
        return False

    def _resolve_request_context(payload: dict[str, Any]) -> dict[str, Any]:
        user_id = _pick_value(payload, "user_id", "userId")
        tenant_key = _pick_value(payload, "tenant_key", "tenantId", "tenant_id") or user_id or "__default__"
        session_key = _pick_value(payload, "session_key", "sessionId") or f"main:{tenant_key}"
        conversation_id = _pick_value(
            payload,
            "conversation_id",
            "conversationId",
            "chat_id",
            "chatId",
            "thread_id",
            "threadId",
        ) or session_key
        message_id = _pick_value(payload, "message_id", "messageId")
        return {
            "tenant_key": tenant_key,
            "user_id": user_id or tenant_key,
            "session_key": session_key,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "media": _pick_media(payload),
            "debug_trace": _pick_bool(payload, "debug_trace", "debugTrace", "debug"),
        }

    async def _run_turn(payload: dict[str, Any]) -> JSONResponse:
        message = _pick_value(payload, "message", "content", "text")
        if not message:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Missing required field: message",
                    "accepted_fields": ["message", "content", "text"],
                },
                status_code=400,
            )

        context = _resolve_request_context(payload)
        tenant_key = context["tenant_key"]
        conversation_id = context["conversation_id"]
        session_key = context["session_key"]
        message_id = context["message_id"]
        media = context["media"]
        debug_trace = bool(context["debug_trace"])

        response = await agent_loop.process_direct(
            message,
            session_key=session_key,
            channel="api",
            chat_id=conversation_id,
            tenant_key=tenant_key,
            media=media,
            message_id=message_id,
            track_primary_session=True,
            debug_trace=debug_trace,
        )
        return JSONResponse(
            {
                "ok": True,
                "response": response,
                "tenant_key": tenant_key,
                "chat_id": conversation_id,
                "session_key": session_key,
                "model": agent_loop.model,
            }
        )

    async def _stream_turn(payload: dict[str, Any]) -> StreamingResponse:
        message = _pick_value(payload, "message", "content", "text")
        if not message:
            return StreamingResponse(
                iter([b'event: error\ndata: {"ok": false, "error": "Missing required field: message"}\n\n']),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        context = _resolve_request_context(payload)
        tenant_key = context["tenant_key"]
        conversation_id = context["conversation_id"]
        session_key = context["session_key"]
        message_id = context["message_id"]
        media = context["media"]
        debug_trace = bool(context["debug_trace"])
        request_started_at = time.perf_counter()
        prompt_snapshot_sent_at: float | None = None
        llm_request_started_at: float | None = None
        first_text_delta_at: float | None = None

        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def on_progress(content: str, *, tool_hint: bool = False, text_delta: bool = False) -> None:
            nonlocal first_text_delta_at
            if text_delta:
                event_name = "text_delta"
                if first_text_delta_at is None:
                    first_text_delta_at = time.perf_counter()
            else:
                event_name = "tool_hint" if tool_hint else "progress"
            body = JSONResponse({"content": content}).body.decode()
            await queue.put(f"event: {event_name}\ndata: {body}\n\n")

        async def run_turn() -> None:
            try:
                if debug_trace:
                    await agent_loop._connect_mcp()
                    runtime = agent_loop._get_runtime(tenant_key)
                    session = runtime.sessions.get_or_create(session_key, tenant_key=tenant_key)
                    raw_history = session.get_history(max_messages=0)
                    history = runtime.memory_consolidator.select_history_for_prompt(session)
                    messages = runtime.context.build_messages(
                        history=history,
                        current_message=message,
                        media=media if media else None,
                        channel="api",
                        chat_id=conversation_id,
                        session_metadata=session.metadata,
                    )
                    tool_defs = agent_loop.get_llm_tool_definitions(lane="main")
                    prompt_state = runtime.context.describe_prompt_state(
                        history=history,
                        raw_history=raw_history,
                        session_metadata=session.metadata,
                    )
                    snapshot = _build_prompt_snapshot_data(messages, tool_defs, prompt_state)
                    body = JSONResponse(
                        {
                            "content": _render_prompt_snapshot(messages, tool_defs, prompt_state),
                            "snapshot": snapshot,
                            "prompt_state": prompt_state,
                            "message_count": len(messages),
                            "tool_count": len(tool_defs),
                        }
                    ).body.decode()
                    await queue.put(f"event: debug_prompt\ndata: {body}\n\n")
                    prompt_snapshot_sent_at = time.perf_counter()
                llm_request_started_at = time.perf_counter()
                response = await agent_loop.process_direct(
                    message,
                    session_key=session_key,
                    channel="api",
                    chat_id=conversation_id,
                    tenant_key=tenant_key,
                    media=media,
                    message_id=message_id,
                    on_progress=on_progress,
                    track_primary_session=True,
                    stream_text=True,
                    debug_trace=debug_trace,
                )
                body = JSONResponse(
                    {
                        "ok": True,
                        "response": response,
                        "tenant_key": tenant_key,
                        "chat_id": conversation_id,
                        "session_key": session_key,
                        "model": agent_loop.model,
                    }
                ).body.decode()
                final_response_at = time.perf_counter()
                if debug_trace:
                    timing_body = JSONResponse(
                        {
                            "timing": _build_turn_timing_payload(
                                request_started_at=request_started_at,
                                prompt_snapshot_sent_at=prompt_snapshot_sent_at,
                                llm_request_started_at=llm_request_started_at,
                                first_text_delta_at=first_text_delta_at,
                                final_response_at=final_response_at,
                                llm_debug=agent_loop.get_last_llm_debug(),
                            ),
                        }
                    ).body.decode()
                    await queue.put(f"event: debug_timing\ndata: {timing_body}\n\n")
                await queue.put(f"event: final\ndata: {body}\n\n")
            except Exception as exc:
                body = JSONResponse({"ok": False, "error": str(exc)}).body.decode()
                await queue.put(f"event: error\ndata: {body}\n\n")
            finally:
                await queue.put(None)

        async def event_stream():
            yield "event: accepted\ndata: {\"ok\": true}\n\n"
            task = asyncio.create_task(run_turn())
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    yield item
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    async def home(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "service": "nanobot-api",
                "model": agent_loop.model,
                "routes": {
                    "health": "GET /health",
                    "turn_get": "GET /turn?message=hello&tenant_key=t1&session_key=main:t1",
                    "turn_post": "POST /turn",
                    "turn_stream_get": "GET /turn/stream?message=hello&tenant_key=t1&session_key=main:t1",
                    "turn_stream_post": "POST /turn/stream",
                    "events_stream_get": "GET /events/stream?tenant_key=t1&session_key=main:t1",
                },
                "post_example": {
                    "message": "你好",
                    "tenant_key": "tenant-demo",
                    "session_key": "main:tenant-demo",
                    "message_id": "msg-001",
                    "images": [
                        "https://example.com/demo.jpg"
                    ],
                },
            }
        )

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "status": "healthy", "model": agent_loop.model})

    async def turn_get(request: Request) -> JSONResponse:
        return await _run_turn(dict(request.query_params))

    async def turn_post(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "Request body must be valid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "Request JSON must be an object"}, status_code=400)
        return await _run_turn(payload)

    async def turn_stream_get(request: Request) -> StreamingResponse:
        return await _stream_turn(dict(request.query_params))

    async def turn_stream_post(request: Request) -> StreamingResponse:
        try:
            payload = await request.json()
        except Exception:
            return StreamingResponse(
                iter([b'event: error\ndata: {"ok": false, "error": "Request body must be valid JSON"}\n\n']),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        if not isinstance(payload, dict):
            return StreamingResponse(
                iter([b'event: error\ndata: {"ok": false, "error": "Request JSON must be an object"}\n\n']),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )
        return await _stream_turn(payload)

    async def events_stream(request: Request) -> StreamingResponse:
        params = dict(request.query_params)
        context = _resolve_request_context(params)
        tenant_key = context["tenant_key"]
        conversation_id = context["conversation_id"]
        session_key = context["session_key"]

        async def event_stream():
            queue, replay = await event_hub.subscribe(
                tenant_key=tenant_key,
                conversation_id=conversation_id,
            )
            try:
                yield _encode_sse(
                    "accepted",
                    {
                        "ok": True,
                        "tenant_key": tenant_key,
                        "chat_id": conversation_id,
                        "session_key": session_key,
                    },
                )
                for event in replay:
                    yield _encode_sse(event.event, event.payload)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield _encode_sse(event.event, event.payload)
            finally:
                await event_hub.unsubscribe(
                    tenant_key=tenant_key,
                    conversation_id=conversation_id,
                    queue=queue,
                )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        outbound_task = asyncio.create_task(
            runtime.run_outbound_worker(
                event_hub.publish_outbound,
                consumer_group=outbound_consumer_group,
            )
        )
        try:
            yield
        finally:
            outbound_task.cancel()
            with suppress(asyncio.CancelledError):
                await outbound_task
            await runtime.close()

    api_app = Starlette(
        debug=verbose,
        routes=[
            Route("/", home, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/turn", turn_get, methods=["GET"]),
            Route("/turn", turn_post, methods=["POST"]),
            Route("/turn/stream", turn_stream_get, methods=["GET"]),
            Route("/turn/stream", turn_stream_post, methods=["POST"]),
            Route("/events/stream", events_stream, methods=["GET"]),
        ],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            )
        ],
        lifespan=lifespan,
    )

    console.print(f"{logo} Starting nanobot API on http://{host}:{port}")
    console.print(
        "[dim]GET /health, GET /turn, POST /turn, GET /turn/stream, POST /turn/stream, GET /events/stream[/dim]"
    )
    uvicorn.run(api_app, host=host, port=port, log_level="debug" if verbose else "info")
