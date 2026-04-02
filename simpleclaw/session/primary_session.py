"""Tenant -> primary session routing helpers."""

from __future__ import annotations

from dataclasses import dataclass

from simpleclaw.storage.interfaces import TenantStateStore
from simpleclaw.tenant.state import TenantStateRepository


@dataclass
class PrimarySessionRoute:
    """Stable main-session pointer for one tenant."""

    tenant_key: str
    session_key: str
    channel: str
    chat_id: str
    updated_at: str


class PrimarySessionStore:
    """Persist and resolve the current primary session for each tenant."""

    def __init__(self, repository: TenantStateStore | None = None) -> None:
        self._repository = repository or TenantStateRepository()

    def save(
        self,
        *,
        tenant_key: str | None,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> None:
        """Save the main session route for one tenant."""
        self._repository.touch_interaction(
            tenant_key=tenant_key,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
        )

    def load(self, tenant_key: str | None) -> PrimarySessionRoute | None:
        """Load the main session route for one tenant."""
        state = self._repository.get(tenant_key)
        if state is None:
            return None
        if not (state.primary_session_key and state.primary_channel and state.primary_chat_id):
            return None
        return PrimarySessionRoute(
            tenant_key=state.tenant_key,
            session_key=state.primary_session_key,
            channel=state.primary_channel,
            chat_id=state.primary_chat_id,
            updated_at=str(state.updated_at_ms),
        )
