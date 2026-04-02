"""Spawn tool for creating background subagents."""

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from simpleclaw.agent.tools.base import Tool

if TYPE_CHECKING:
    from simpleclaw.agent.subagent import SubagentManager


class SpawnTool(Tool):
    """Tool to spawn a subagent for background task execution."""

    def __init__(self, manager: "SubagentManager"):
        self._default_manager = manager
        self._manager_ctx: ContextVar["SubagentManager"] = ContextVar("spawn_manager", default=manager)
        self._origin_channel_ctx: ContextVar[str] = ContextVar("spawn_origin_channel", default="cli")
        self._origin_chat_id_ctx: ContextVar[str] = ContextVar("spawn_origin_chat_id", default="direct")
        self._session_key_ctx: ContextVar[str] = ContextVar("spawn_session_key", default="cli:direct")
        self._tenant_key_ctx: ContextVar[str] = ContextVar("spawn_tenant_key", default="__default__")

    def set_context(
        self,
        channel: str,
        chat_id: str,
        session_key: str | None = None,
        tenant_key: str | None = None,
        manager: "SubagentManager | None" = None,
    ) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel_ctx.set(channel)
        self._origin_chat_id_ctx.set(chat_id)
        self._session_key_ctx.set(session_key or f"{channel}:{chat_id}")
        self._tenant_key_ctx.set(tenant_key or "__default__")
        self._manager_ctx.set(manager or self._default_manager)

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return "Run a background subagent for an independent task."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Subagent task",
                },
                "label": {
                    "type": "string",
                    "description": "Short task label",
                },
            },
            "required": ["task"],
        }

    async def execute(self, task: str, label: str | None = None, **kwargs: Any) -> str:
        """Spawn a subagent to execute the given task."""
        return await self._manager_ctx.get().spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel_ctx.get(),
            origin_chat_id=self._origin_chat_id_ctx.get(),
            session_key=self._session_key_ctx.get(),
            tenant_key=self._tenant_key_ctx.get(),
        )
