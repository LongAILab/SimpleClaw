"""Message bus module for decoupled channel-agent communication."""

from simpleclaw.bus.events import InboundMessage, OutboundMessage
from simpleclaw.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
