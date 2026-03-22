"""Message tool for sending messages to users."""

from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel_ctx: ContextVar[str] = ContextVar("message_default_channel", default=default_channel)
        self._default_chat_id_ctx: ContextVar[str] = ContextVar("message_default_chat_id", default=default_chat_id)
        self._default_message_id_ctx: ContextVar[str | None] = ContextVar(
            "message_default_message_id", default=default_message_id
        )
        self._default_session_key_ctx: ContextVar[str | None] = ContextVar(
            "message_default_session_key", default=None
        )
        self._default_tenant_key_ctx: ContextVar[str | None] = ContextVar(
            "message_default_tenant_key", default=None
        )
        self._default_metadata_ctx: ContextVar[dict[str, Any] | None] = ContextVar(
            "message_default_metadata", default=None
        )
        self._sent_in_turn_ctx: ContextVar[bool] = ContextVar("message_sent_in_turn", default=False)

    def set_context(
        self,
        channel: str,
        chat_id: str,
        message_id: str | None = None,
        *,
        session_key: str | None = None,
        tenant_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Set the current message context."""
        self._default_channel_ctx.set(channel)
        self._default_chat_id_ctx.set(chat_id)
        self._default_message_id_ctx.set(message_id)
        self._default_session_key_ctx.set(session_key)
        self._default_tenant_key_ctx.set(tenant_key)
        self._default_metadata_ctx.set(dict(metadata or {}))

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn_ctx.set(False)

    def sent_in_turn(self) -> bool:
        """Return whether the tool already sent a message in the current task."""
        return self._sent_in_turn_ctx.get()

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a message to a chat."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Message text"
                },
                "channel": {
                    "type": "string",
                    "description": "Target channel"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Target chat ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Attachment file paths"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any
    ) -> str:
        default_channel = self._default_channel_ctx.get()
        default_chat_id = self._default_chat_id_ctx.get()
        default_message_id = self._default_message_id_ctx.get()
        default_session_key = self._default_session_key_ctx.get()
        default_tenant_key = self._default_tenant_key_ctx.get()
        default_metadata = dict(self._default_metadata_ctx.get() or {})
        channel = channel or default_channel
        chat_id = chat_id or default_chat_id
        message_id = message_id or default_message_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            tenant_key=default_tenant_key,
            session_key=default_session_key,
            media=media or [],
            metadata={
                **default_metadata,
                "message_id": message_id,
            },
        )

        try:
            await self._send_callback(msg)
            if channel == default_channel and chat_id == default_chat_id:
                self._sent_in_turn_ctx.set(True)
            media_info = f" with {len(media)} attachments" if media else ""
            return f"Message sent to {channel}:{chat_id}{media_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
