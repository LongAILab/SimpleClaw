"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    tenant_key: str | None = None  # Explicit tenant / user namespace
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return self.session_key_override or f"{self.channel}:{self.chat_id}"

    @property
    def effective_tenant_key(self) -> str:
        """Stable tenant key used for storage and scheduling."""
        return self.tenant_key or "__default__"

    @property
    def routing_key(self) -> str:
        """Unique key for ordered processing within one tenant/session."""
        return f"{self.effective_tenant_key}:{self.session_key}"

    @property
    def lane(self) -> str:
        """Execution lane used for global concurrency control."""
        lane = self.metadata.get("_lane")
        return str(lane).strip() if lane else "main"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    channel: str
    chat_id: str
    content: str
    tenant_key: str | None = None
    session_key: str | None = None
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


