"""Tests for the VolcEngine Responses provider."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rich.console import Console

from nanobot.config.schema import Config
from nanobot.providers.base import GenerationSettings
from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.providers.volcengine_responses_provider import VolcengineResponsesProvider
from nanobot.runtime.agent_factory import make_provider


class _FakeUsage:
    def __init__(self, *, input_tokens: int, output_tokens: int, total_tokens: int, cached_tokens: int = 0):
        self._payload = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens},
        }

    def model_dump(self) -> dict:
        return dict(self._payload)


class _FakeText:
    type = "output_text"

    def __init__(self, text: str):
        self.text = text


class _FakeMessage:
    type = "message"

    def __init__(self, text: str):
        self.content = [_FakeText(text)]


class _FakeFunctionCall:
    type = "function_call"

    def __init__(self, *, call_id: str, name: str, arguments: str):
        self.call_id = call_id
        self.name = name
        self.arguments = arguments


class _FakeResponse:
    def __init__(
        self,
        *,
        response_id: str,
        output: list[object] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cached_tokens: int = 0,
    ):
        self.id = response_id
        self.output = output or []
        self.usage = _FakeUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_tokens=cached_tokens,
        )
        self.status = "completed"
        self.error = None


class _FakeResponsesAPI:
    def __init__(self, responses: list[_FakeResponse]):
        self.calls: list[dict] = []
        self._responses = list(responses)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _make_provider(responses: list[_FakeResponse]) -> tuple[VolcengineResponsesProvider, _FakeResponsesAPI]:
    api = _FakeResponsesAPI(responses)
    client = SimpleNamespace(responses=api)
    provider = VolcengineResponsesProvider(
        api_key="test-key",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        default_model="doubao-seed-2-0-mini-260215",
        client=client,
    )
    provider.generation = GenerationSettings(thinking=False)
    return provider, api


def _messages() -> list[dict]:
    return [
        {
            "role": "system",
            "content": "stable prefix\n\n---\n\ndynamic tail",
            "_cache_stable_prefix": "stable prefix",
            "_cache_dynamic_tail": "dynamic tail",
            "_cache_tenant_key": "tenant-a",
        },
        {"role": "user", "content": "你好"},
    ]


def _tools(version: str = "v1") -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": f"weather {version}",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                    },
                    "required": ["city"],
                },
            },
        }
    ]


def test_prefix_cache_is_created_once_and_reused() -> None:
    provider, api = _make_provider(
        [
            _FakeResponse(response_id="resp_prefix_1", input_tokens=300, total_tokens=300),
            _FakeResponse(
                response_id="resp_turn_1",
                output=[_FakeMessage("第一次回复")],
                input_tokens=320,
                output_tokens=16,
                total_tokens=336,
                cached_tokens=300,
            ),
            _FakeResponse(
                response_id="resp_turn_2",
                output=[_FakeFunctionCall(call_id="call_1", name="weather", arguments='{"city":"广州"}')],
                input_tokens=324,
                output_tokens=8,
                total_tokens=332,
                cached_tokens=300,
            ),
        ]
    )

    first = asyncio.run(provider.chat(messages=_messages(), tools=_tools()))
    second = asyncio.run(provider.chat(messages=_messages(), tools=_tools()))

    assert first.content == "第一次回复"
    assert first.usage["cached_tokens"] == 300
    assert first.usage["cache"]["created"] is True
    assert first.usage["cache"]["reused"] is False

    assert second.has_tool_calls is True
    assert second.tool_calls[0].name == "weather"
    assert second.usage["cache"]["created"] is False
    assert second.usage["cache"]["reused"] is True

    assert len(api.calls) == 3
    assert api.calls[0]["extra_body"]["caching"]["prefix"] is True
    assert api.calls[0]["tools"][0]["name"] == "weather"
    assert api.calls[1]["previous_response_id"] == "resp_prefix_1"
    assert "tools" not in api.calls[1]
    assert api.calls[2]["previous_response_id"] == "resp_prefix_1"
    assert "tools" not in api.calls[2]


def test_prefix_cache_rebuilds_when_tool_schema_changes() -> None:
    provider, api = _make_provider(
        [
            _FakeResponse(response_id="resp_prefix_1", input_tokens=300, total_tokens=300),
            _FakeResponse(
                response_id="resp_turn_1",
                output=[_FakeMessage("ok-1")],
                input_tokens=320,
                output_tokens=10,
                total_tokens=330,
                cached_tokens=300,
            ),
            _FakeResponse(response_id="resp_prefix_2", input_tokens=305, total_tokens=305),
            _FakeResponse(
                response_id="resp_turn_2",
                output=[_FakeMessage("ok-2")],
                input_tokens=325,
                output_tokens=10,
                total_tokens=335,
                cached_tokens=305,
            ),
        ]
    )

    first = asyncio.run(provider.chat(messages=_messages(), tools=_tools("v1")))
    second = asyncio.run(provider.chat(messages=_messages(), tools=_tools("v2")))

    assert first.content == "ok-1"
    assert second.content == "ok-2"
    assert second.usage["cache"]["created"] is True
    assert second.usage["cache"]["response_id"] == "resp_prefix_2"

    assert len(api.calls) == 4
    assert api.calls[2]["extra_body"]["caching"]["prefix"] is True
    assert api.calls[2]["tools"][0]["description"] == "weather v2"
    assert api.calls[3]["previous_response_id"] == "resp_prefix_2"


def test_make_provider_uses_responses_only_for_main_defaults() -> None:
    config = Config()
    config.agents.defaults.model = "doubao-seed-2-0-mini-260215"
    config.agents.defaults.provider = "volcengine"
    config.agents.defaults.responses_prefix_cache = True
    config.providers.volcengine.api_key = "test-key"
    console = Console(record=True)

    main_provider = make_provider(config, console, config.agents.defaults)
    cron_provider = make_provider(config, console, config.agents.cron)

    assert isinstance(main_provider, VolcengineResponsesProvider)
    assert isinstance(cron_provider, LiteLLMProvider)
