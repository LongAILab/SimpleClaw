from nanobot.providers.base import GenerationSettings
from nanobot.providers.litellm_provider import LiteLLMProvider


def test_moonshot_kimi_k25_disables_thinking_explicitly() -> None:
    provider = LiteLLMProvider(default_model="kimi-k2.5")
    kwargs: dict = {"temperature": 0.1}

    provider._apply_provider_request_overrides(
        "kimi-k2.5",
        "moonshot/kimi-k2.5",
        kwargs,
    )

    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["temperature"] == 0.6


def test_gateway_routed_kimi_does_not_force_moonshot_thinking_flag() -> None:
    provider = LiteLLMProvider(default_model="kimi-k2.5", provider_name="openrouter")
    kwargs: dict = {}

    provider._apply_provider_request_overrides(
        "kimi-k2.5",
        "openrouter/kimi-k2.5",
        kwargs,
    )

    assert "extra_body" not in kwargs


def test_explicit_thinking_true_keeps_kimi_default_mode() -> None:
    provider = LiteLLMProvider(default_model="kimi-k2.5")
    provider.generation = GenerationSettings(thinking=True)
    kwargs: dict = {"temperature": 0.1}

    provider._apply_provider_request_overrides(
        "kimi-k2.5",
        "moonshot/kimi-k2.5",
        kwargs,
    )

    assert "extra_body" not in kwargs
    assert kwargs["temperature"] == 0.1
