"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

import uuid
from typing import Any

import json_repair
from openai import AsyncOpenAI

from nanobot.providers.base import LLMProvider, LLMResponse, LLMStreamEvent, ToolCallRequest


class CustomProvider(LLMProvider):

    def __init__(self, api_key: str = "no-key", api_base: str = "http://localhost:8000/v1", default_model: str = "default"):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        # Keep affinity stable for this provider instance to improve backend cache locality.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            default_headers={"x-session-affinity": uuid.uuid4().hex},
        )

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
                   reasoning_effort: str | None = None,
                   tool_choice: str | dict[str, Any] | None = None) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice or "auto")
        try:
            return self._parse(await self._client.chat.completions.create(**kwargs))
        except Exception as e:
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

    async def stream_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                          model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
                          reasoning_effort: str | None = None,
                          tool_choice: str | dict[str, Any] | None = None):
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
            "stream": True,
        }
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if tools:
            kwargs.update(tools=tools, tool_choice=tool_choice or "auto")

        content_parts: list[str] = []
        tool_buffers: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: dict[str, int] = {}

        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                for choice in getattr(chunk, "choices", []) or []:
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None) if delta is not None else None
                    if isinstance(text, str) and text:
                        content_parts.append(text)
                        yield LLMStreamEvent(event="text_delta", text=text)

                    delta_tool_calls = getattr(delta, "tool_calls", None) or []
                    for tc in delta_tool_calls:
                        idx = getattr(tc, "index", len(tool_buffers))
                        buf = tool_buffers.setdefault(
                            idx,
                            {
                                "id": getattr(tc, "id", None) or f"call_{idx}",
                                "name": None,
                                "arguments": "",
                            },
                        )
                        function = getattr(tc, "function", None)
                        if function is not None:
                            name = getattr(function, "name", None)
                            if name:
                                buf["name"] = name
                            arguments = getattr(function, "arguments", None)
                            if arguments:
                                buf["arguments"] += arguments
                    finish_reason = getattr(choice, "finish_reason", None) or finish_reason

                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage = {
                        "prompt_tokens": getattr(chunk_usage, "prompt_tokens", 0),
                        "completion_tokens": getattr(chunk_usage, "completion_tokens", 0),
                        "total_tokens": getattr(chunk_usage, "total_tokens", 0),
                    }
        except Exception as e:
            yield LLMStreamEvent(event="response", response=LLMResponse(content=f"Error: {e}", finish_reason="error"))
            return

        tool_calls = [
            ToolCallRequest(
                id=buf["id"],
                name=buf.get("name") or "unknown_tool",
                arguments=json_repair.loads(buf["arguments"]) if isinstance(buf["arguments"], str) and buf["arguments"] else {},
            )
            for _, buf in sorted(tool_buffers.items())
        ]
        yield LLMStreamEvent(
            event="response",
            response=LLMResponse(
                content="".join(content_parts) or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason or "stop",
                usage=usage,
            ),
        )

    def _parse(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        msg = choice.message
        tool_calls = [
            ToolCallRequest(id=tc.id, name=tc.function.name,
                            arguments=json_repair.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments)
            for tc in (msg.tool_calls or [])
        ]
        u = response.usage
        return LLMResponse(
            content=msg.content, tool_calls=tool_calls, finish_reason=choice.finish_reason or "stop",
            usage={"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens, "total_tokens": u.total_tokens} if u else {},
            reasoning_content=getattr(msg, "reasoning_content", None) or None,
        )

    def get_default_model(self) -> str:
        return self.default_model

