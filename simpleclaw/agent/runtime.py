"""Tenant-scoped runtime state for the agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from simpleclaw.config.paths import get_tenant_workspace_path
from simpleclaw.utils.helpers import sync_workspace_templates

if TYPE_CHECKING:
    from simpleclaw.agent.context import ContextBuilder
    from simpleclaw.agent.memory import MemoryConsolidator
    from simpleclaw.agent.subagent import SubagentManager
    from simpleclaw.providers.base import LLMProvider
    from simpleclaw.session.manager import SessionManager


@dataclass
class TenantRuntime:
    """Tenant-scoped runtime components."""

    tenant_key: str
    workspace: Path
    context: ContextBuilder
    sessions: SessionManager
    subagents: SubagentManager
    memory_consolidator: MemoryConsolidator


class TenantRuntimeManager:
    """Create and cache tenant-scoped runtime components."""

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        context_window_tokens: int,
        tools_get_definitions: Callable[[], list[dict]],
        create_subagent_manager: Callable[[Path], SubagentManager],
        create_context_builder: Callable[[Path, str], ContextBuilder],
        create_session_manager: Callable[[Path, str], SessionManager],
        create_memory_consolidator: Callable[[Path, str, SessionManager, ContextBuilder], MemoryConsolidator],
        provision_tenant: Callable[[str], None] | None = None,
        ensure_tenant_workspace: bool = True,
    ) -> None:
        self._workspace = workspace
        self._provider = provider
        self._model = model
        self._context_window_tokens = context_window_tokens
        self._tools_get_definitions = tools_get_definitions
        self._create_subagent_manager = create_subagent_manager
        self._create_context_builder = create_context_builder
        self._create_session_manager = create_session_manager
        self._create_memory_consolidator = create_memory_consolidator
        self._provision_tenant = provision_tenant
        self._ensure_tenant_workspace = ensure_tenant_workspace
        self.runtimes: dict[str, TenantRuntime] = {}

    def get_runtime(self, tenant_key: str | None) -> TenantRuntime:
        """Return a tenant-scoped runtime, creating it lazily."""
        effective_tenant = tenant_key or "__default__"
        if runtime := self.runtimes.get(effective_tenant):
            return runtime

        sync_workspace_templates(self._workspace, silent=True)
        if self._provision_tenant is not None:
            self._provision_tenant(effective_tenant)
        workspace = get_tenant_workspace_path(
            self._workspace,
            effective_tenant,
            create=self._ensure_tenant_workspace or effective_tenant == "__default__",
        )
        if effective_tenant == "__default__":
            sync_workspace_templates(workspace, silent=True)
        sessions = self._create_session_manager(workspace, effective_tenant)
        context = self._create_context_builder(workspace, effective_tenant)
        runtime = TenantRuntime(
            tenant_key=effective_tenant,
            workspace=workspace,
            context=context,
            sessions=sessions,
            subagents=self._create_subagent_manager(workspace),
            memory_consolidator=self._create_memory_consolidator(workspace, effective_tenant, sessions, context),
        )
        self.runtimes[effective_tenant] = runtime
        return runtime
