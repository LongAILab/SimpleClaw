"""Shared agent/provider construction helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import typer
from rich.console import Console

from nanobot.bus.queue import MessageBus
from nanobot.config.schema import (
    AgentDefaults,
    Config,
    CronAgentConfig,
    HeartbeatAgentConfig,
    PostprocessAgentConfig,
)
from nanobot.session.manager import SessionManager


def resolve_agent_settings(
    config: Config,
    settings: AgentDefaults | CronAgentConfig | HeartbeatAgentConfig | PostprocessAgentConfig | None = None,
) -> AgentDefaults:
    """Resolve execution settings against the main agent defaults."""
    if settings is None:
        return config.agents.defaults
    if isinstance(settings, (CronAgentConfig, HeartbeatAgentConfig, PostprocessAgentConfig)):
        return settings.resolve(config.agents.defaults)
    return settings


def make_provider(
    config: Config,
    console: Console,
    settings: AgentDefaults | CronAgentConfig | HeartbeatAgentConfig | PostprocessAgentConfig | None = None,
):
    """Create the appropriate LLM provider from config."""
    from nanobot.providers.azure_openai_provider import AzureOpenAIProvider
    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

    resolved = resolve_agent_settings(config, settings)
    model = resolved.model
    provider_name = config.get_provider_name(model, provider_override=resolved.provider)
    provider_cfg = config.get_provider(model, provider_override=resolved.provider)

    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        provider = OpenAICodexProvider(default_model=model)
    elif provider_name == "custom":
        from nanobot.providers.custom_provider import CustomProvider

        provider = CustomProvider(
            api_key=provider_cfg.api_key if provider_cfg else "no-key",
            api_base=(
                config.get_api_base(model, provider_override=resolved.provider) or "http://localhost:8000/v1"
            ),
            default_model=model,
        )
    elif provider_name == "azure_openai":
        if not provider_cfg or not provider_cfg.api_key or not provider_cfg.api_base:
            console.print("[red]Error: Azure OpenAI requires api_key and api_base.[/red]")
            raise typer.Exit(1)
        provider = AzureOpenAIProvider(
            api_key=provider_cfg.api_key,
            api_base=provider_cfg.api_base,
            default_model=model,
        )
    else:
        from nanobot.providers.litellm_provider import LiteLLMProvider
        from nanobot.providers.registry import find_by_name

        spec = find_by_name(provider_name)
        if not model.startswith("bedrock/") and not (provider_cfg and provider_cfg.api_key) and not (
            spec and (spec.is_oauth or spec.is_local)
        ):
            console.print("[red]Error: No API key configured.[/red]")
            raise typer.Exit(1)
        provider = LiteLLMProvider(
            api_key=provider_cfg.api_key if provider_cfg else None,
            api_base=config.get_api_base(model, provider_override=resolved.provider),
            default_model=model,
            extra_headers=provider_cfg.extra_headers if provider_cfg else None,
            provider_name=provider_name,
        )

    provider.generation = GenerationSettings(
        temperature=resolved.temperature,
        max_tokens=resolved.max_tokens,
        reasoning_effort=resolved.reasoning_effort,
    )
    return provider


def make_agent_loop(
    config: Config,
    console: Console,
    bus: MessageBus,
    *,
    cron_service=None,
    session_manager: SessionManager | None = None,
    settings: AgentDefaults | CronAgentConfig | HeartbeatAgentConfig | PostprocessAgentConfig | None = None,
    tenant_state_repo=None,
    lease_repo=None,
    create_session_manager: Callable[[Path, str], SessionManager] | None = None,
    create_context_builder: Callable[[Path, str], Any] | None = None,
    create_memory_consolidator: Callable[[Path, str, SessionManager, Any], Any] | None = None,
    provision_tenant: Callable[[str], None] | None = None,
    ensure_tenant_workspace: bool = True,
    document_store=None,
    memory_store=None,
):
    """Create an AgentLoop for the given execution settings."""
    from nanobot.agent.loop import AgentLoop

    resolved = resolve_agent_settings(config, settings)
    provider = make_provider(config, console, resolved)
    lane_limits = {
        "main": config.gateway.lanes.main,
        "cron": config.gateway.lanes.cron,
        "heartbeat": config.gateway.lanes.heartbeat,
        "subagent": config.gateway.lanes.subagent,
    }
    lane_backlog_caps = {
        "main": config.gateway.lanes.main_backlog,
    }
    lane_tenant_limits = {
        "main": config.gateway.lanes.main_per_tenant,
    }
    postprocess = config.gateway.postprocess
    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=resolved.model,
        max_iterations=resolved.max_tool_iterations,
        context_window_tokens=resolved.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron_service,
        cron_deliver_default=config.agents.cron.deliver_default,
        cron_execution_policy_default=config.agents.cron.execution_policy,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        create_session_manager=create_session_manager,
        create_context_builder=create_context_builder,
        create_memory_consolidator=create_memory_consolidator,
        provision_tenant=provision_tenant,
        ensure_tenant_workspace=ensure_tenant_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        tenant_state_repo=tenant_state_repo,
        lease_repo=lease_repo,
        document_store=document_store,
        memory_store=memory_store,
        lane_limits=lane_limits,
        lane_backlog_caps=lane_backlog_caps,
        lane_tenant_limits=lane_tenant_limits,
        postprocess_enabled=postprocess.enabled,
        postprocess_max_concurrency=postprocess.max_concurrency,
        defer_cron_writes=postprocess.defer_cron_writes,
        defer_heartbeat_writes=postprocess.defer_heartbeat_writes,
        defer_memory_writes=postprocess.defer_memory_writes,
        structured_memory_enabled=postprocess.structured_memory_enabled,
        structured_memory_recent_messages=postprocess.structured_memory_recent_messages,
        session_ownership_ttl_s=config.runtime.tasks.ownership_ttl_s,
    )
