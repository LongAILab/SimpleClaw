"""VolcEngine Responses API provider with explicit prefix-cache reuse."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import json_repair
from loguru import logger
from openai import AsyncOpenAI

from simpleclaw.providers.base import LLMProvider, LLMResponse, LLMStreamEvent, ToolCallRequest


@dataclass(slots=True)
class PrefixCacheEntry:
    """In-memory handle for one VolcEngine explicit prefix cache."""

    response_id: str
    created_at: float
    expire_at: float
    prefix_hash: str


class VolcengineResponsesProvider(LLMProvider):
    """Direct VolcEngine Responses API provider for explicit common-prefix caching."""

    _CACHE_TTL_S = 60 * 60

    def __init__(
        self,
        api_key: str,
        api_base: str | None = None,
        default_model: str = "doubao-seed-2-0-mini-260215",
        extra_headers: dict[str, str] | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        default_headers = dict(self.extra_headers)
        default_headers.setdefault("x-session-affinity", uuid.uuid4().hex)
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            default_headers=default_headers,
        )
        self._prefix_cache: dict[str, PrefixCacheEntry] = {}
        self._prefix_lock = asyncio.Lock()

    @staticmethod
    def _hash_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _hash_json(cls, value: Any) -> str:
        return cls._hash_text(json.dumps(value, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _tool_choice_for_responses(tool_choice: str | dict[str, Any] | None) -> str | dict[str, Any] | None:
        if tool_choice is None or isinstance(tool_choice, str):
            return tool_choice
        function = tool_choice.get("function") or {}
        name = function.get("name")
        if isinstance(name, str) and name.strip():
            return {"type": "function", "name": name.strip()}
        return None

    @staticmethod
    def _extract_prompt_parts(messages: list[dict[str, Any]]) -> tuple[str, str, str]:
        if not messages:
            return "__default__", "", ""
        first = messages[0]
        if str(first.get("role") or "").lower() != "system":
            return "__default__", "", ""
        tenant_key = str(first.get("_cache_tenant_key") or "__default__")
        stable_prefix = str(first.get("_cache_stable_prefix") or first.get("content") or "")
        dynamic_tail = str(first.get("_cache_dynamic_tail") or "")
        return tenant_key, stable_prefix, dynamic_tail

    @staticmethod
    def _build_text_content(text: str) -> dict[str, Any]:
        return {"type": "input_text", "text": text}

    @classmethod
    def _convert_content(cls, content: Any) -> str | list[dict[str, Any]]:
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            return json.dumps(content, ensure_ascii=False)

        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                converted.append(cls._build_text_content(str(item)))
                continue
            item_type = item.get("type")
            if item_type == "text":
                converted.append(cls._build_text_content(str(item.get("text") or "")))
            elif item_type == "image_url":
                image = item.get("image_url") or {}
                url = image.get("url")
                if isinstance(url, str) and url:
                    converted.append({"type": "input_image", "image_url": url})
            else:
                converted.append(cls._build_text_content(json.dumps(item, ensure_ascii=False)))
        return converted or ""

    @classmethod
    def _convert_messages_to_input(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        input_items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "")
            if role == "tool":
                output = message.get("content")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(message.get("tool_call_id") or ""),
                        "output": output or "",
                        "status": "completed",
                    }
                )
                continue

            if role == "assistant" and isinstance(message.get("tool_calls"), list):
                content = message.get("content")
                if content:
                    input_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": cls._convert_content(content),
                        }
                    )
                for tool_call in message["tool_calls"]:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function") or {}
                    arguments = function.get("arguments") or "{}"
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": str(tool_call.get("id") or ""),
                            "name": str(function.get("name") or "unknown_tool"),
                            "arguments": arguments,
                            "status": "completed",
                        }
                    )
                continue

            input_items.append(
                {
                    "type": "message",
                    "role": role,
                    "content": cls._convert_content(message.get("content")),
                }
            )
        return input_items

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        converted: list[dict[str, Any]] = []
        for tool in tools:
            function = tool.get("function") or {}
            converted.append(
                {
                    "type": "function",
                    "name": function.get("name"),
                    "description": function.get("description"),
                    "parameters": function.get("parameters") or {},
                    "strict": False,
                }
            )
        return converted

    def _build_extra_body(self, *, cache_prefix: bool = False, cache_enabled: bool = False) -> dict[str, Any] | None:
        extra_body: dict[str, Any] = {}
        if cache_enabled:
            caching: dict[str, Any] = {"type": "enabled"}
            if cache_prefix:
                caching["prefix"] = True
            extra_body["caching"] = caching
        if self.generation.thinking is not True:
            extra_body["thinking"] = {"type": "disabled"}
        return extra_body or None

    def _build_request_kwargs(
        self,
        *,
        model: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
        tool_choice: str | dict[str, Any] | None,
        previous_response_id: str | None = None,
        cache_enabled: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "max_output_tokens": max(1, max_tokens),
            "temperature": temperature,
        }
        extra_body = self._build_extra_body(cache_enabled=cache_enabled)
        if extra_body:
            kwargs["extra_body"] = extra_body
        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
            kwargs["parallel_tool_calls"] = True
        return kwargs

    async def _create_prefix_cache(
        self,
        *,
        model: str,
        stable_prefix: str,
        tools: list[dict[str, Any]] | None,
    ) -> PrefixCacheEntry:
        response = await self._client.responses.create(
            model=model,
            input=[{"type": "message", "role": "system", "content": stable_prefix}],
            tools=tools,
            extra_body=self._build_extra_body(cache_prefix=True, cache_enabled=True),
        )
        response_id = getattr(response, "id", None)
        if not isinstance(response_id, str) or not response_id:
            raise RuntimeError("VolcEngine prefix cache creation returned no response id")
        now = time.time()
        return PrefixCacheEntry(
            response_id=response_id,
            created_at=now,
            expire_at=now + self._CACHE_TTL_S,
            prefix_hash=self._hash_text(stable_prefix),
        )

    async def _ensure_prefix_cache(
        self,
        *,
        tenant_key: str,
        model: str,
        stable_prefix: str,
        tools: list[dict[str, Any]] | None,
    ) -> tuple[PrefixCacheEntry | None, dict[str, Any]]:
        if not stable_prefix.strip():
            return None, {
                "enabled": False,
                "created": False,
                "reused": False,
                "reason": "empty_stable_prefix",
            }

        tool_hash = self._hash_json(tools or [])
        prefix_hash = self._hash_text(stable_prefix)
        cache_key = "::".join(
            [
                tenant_key,
                model,
                prefix_hash,
                tool_hash,
                str(self.generation.thinking is True).lower(),
            ]
        )

        async with self._prefix_lock:
            now = time.time()
            entry = self._prefix_cache.get(cache_key)
            if entry is not None and entry.expire_at > now and entry.prefix_hash == prefix_hash:
                return entry, {
                    "enabled": True,
                    "created": False,
                    "reused": True,
                    "cache_key": cache_key,
                    "prefix_hash": prefix_hash,
                    "response_id": entry.response_id,
                    "expire_at": entry.expire_at,
                }

            entry = await self._create_prefix_cache(
                model=model,
                stable_prefix=stable_prefix,
                tools=tools,
            )
            self._prefix_cache[cache_key] = entry
            return entry, {
                "enabled": True,
                "created": True,
                "reused": False,
                "cache_key": cache_key,
                "prefix_hash": prefix_hash,
                "response_id": entry.response_id,
                "expire_at": entry.expire_at,
            }

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, Any]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):
            payload = usage.model_dump()
        elif isinstance(usage, dict):
            payload = dict(usage)
        else:
            payload = {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
            }

        input_tokens = int(payload.get("input_tokens") or payload.get("prompt_tokens") or 0)
        output_tokens = int(payload.get("output_tokens") or payload.get("completion_tokens") or 0)
        total_tokens = int(payload.get("total_tokens") or (input_tokens + output_tokens))
        input_details = payload.get("input_tokens_details") or payload.get("prompt_tokens_details") or {}
        cached_tokens = int(input_details.get("cached_tokens") or 0)

        parsed = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
        }
        if input_details:
            parsed["input_tokens_details"] = input_details
        return parsed

    @classmethod
    def _parse_response(
        cls,
        response: Any,
        *,
        cache_debug: dict[str, Any] | None = None,
    ) -> LLMResponse:
        error = getattr(response, "error", None)
        if error:
            return LLMResponse(
                content=f"Error calling LLM: {error}",
                finish_reason="error",
                usage={"cache": cache_debug} if cache_debug else {},
            )

        content_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for item in getattr(response, "output", None) or []:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for part in getattr(item, "content", None) or []:
                    if getattr(part, "type", None) == "output_text" and getattr(part, "text", None):
                        content_parts.append(str(part.text))
            elif item_type == "function_call":
                arguments = getattr(item, "arguments", None) or "{}"
                try:
                    parsed_arguments = json_repair.loads(arguments) if isinstance(arguments, str) else arguments
                except Exception:
                    parsed_arguments = {"raw": arguments}
                tool_calls.append(
                    ToolCallRequest(
                        id=str(getattr(item, "call_id", None) or getattr(item, "id", "") or uuid.uuid4().hex),
                        name=str(getattr(item, "name", None) or "unknown_tool"),
                        arguments=parsed_arguments,
                    )
                )

        usage = cls._extract_usage(response)
        if cache_debug:
            usage["cache"] = cache_debug

        finish_reason = "tool_calls" if tool_calls else "stop"
        status = str(getattr(response, "status", "") or "")
        if status in {"failed", "incomplete"}:
            finish_reason = "error"
        return LLMResponse(
            content="".join(content_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def _prepare_request(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        max_tokens: int,
        temperature: float,
        tool_choice: str | dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        tenant_key, stable_prefix, dynamic_tail = self._extract_prompt_parts(messages)
        converted_tools = self._convert_tools(tools)
        tool_choice_for_responses = self._tool_choice_for_responses(tool_choice)

        cache_debug: dict[str, Any] = {
            "provider": "volcengine_responses",
            "enabled": False,
            "created": False,
            "reused": False,
        }

        if stable_prefix:
            try:
                entry, cache_debug = await self._ensure_prefix_cache(
                    tenant_key=tenant_key,
                    model=model,
                    stable_prefix=stable_prefix,
                    tools=converted_tools,
                )
            except Exception as exc:
                logger.warning("VolcEngine prefix cache setup failed, falling back to full request: {}", exc)
                entry = None
                cache_debug = {
                    "provider": "volcengine_responses",
                    "enabled": False,
                    "created": False,
                    "reused": False,
                    "fallback_error": str(exc),
                }
            if entry is not None:
                request_messages: list[dict[str, Any]] = []
                if dynamic_tail.strip():
                    request_messages.append({"role": "system", "content": dynamic_tail})
                request_messages.extend(messages[1:])
                return (
                    self._build_request_kwargs(
                        model=model,
                        input_items=self._convert_messages_to_input(request_messages),
                        tools=None,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tool_choice=None,
                        previous_response_id=entry.response_id,
                        cache_enabled=True,
                    ),
                    cache_debug,
                )

        return (
            self._build_request_kwargs(
                model=model,
                input_items=self._convert_messages_to_input(messages),
                tools=converted_tools,
                max_tokens=max_tokens,
                temperature=temperature,
                tool_choice=tool_choice_for_responses,
            ),
            cache_debug,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        del reasoning_effort  # Reserved for future VolcEngine-specific mapping.
        messages = self._sanitize_empty_content(messages)
        resolved_model = model or self.default_model
        try:
            request_kwargs, cache_debug = await self._prepare_request(
                messages=messages,
                tools=tools,
                model=resolved_model,
                max_tokens=max_tokens,
                temperature=temperature,
                tool_choice=tool_choice,
            )
            response = await self._client.responses.create(**request_kwargs)
            return self._parse_response(response, cache_debug=cache_debug)
        except Exception as exc:
            return LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error")

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ):
        del reasoning_effort  # Reserved for future VolcEngine-specific mapping.
        messages = self._sanitize_empty_content(messages)
        resolved_model = model or self.default_model
        try:
            request_kwargs, cache_debug = await self._prepare_request(
                messages=messages,
                tools=tools,
                model=resolved_model,
                max_tokens=max_tokens,
                temperature=temperature,
                tool_choice=tool_choice,
            )
            request_kwargs["stream"] = True
            stream = await self._client.responses.create(**request_kwargs)
            final_response: Any | None = None

            async for event in stream:
                event_type = getattr(event, "type", None)
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", None)
                    if isinstance(delta, str) and delta:
                        yield LLMStreamEvent(event="text_delta", text=delta)
                elif event_type == "response.completed":
                    final_response = getattr(event, "response", None)

            if final_response is None:
                yield LLMStreamEvent(
                    event="response",
                    response=LLMResponse(
                        content="Error calling LLM: stream returned no final response",
                        finish_reason="error",
                    ),
                )
                return

            yield LLMStreamEvent(
                event="response",
                response=self._parse_response(final_response, cache_debug=cache_debug),
            )
        except Exception as exc:
            yield LLMStreamEvent(
                event="response",
                response=LLMResponse(content=f"Error calling LLM: {exc}", finish_reason="error"),
            )

    def get_default_model(self) -> str:
        return self.default_model
