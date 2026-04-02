"""MySQL-backed session manager."""

from __future__ import annotations

from simpleclaw.session.manager import Session, SessionManager
from simpleclaw.config.paths import sanitize_tenant_key
from simpleclaw.storage.mysql import MySQLSessionStore


class MySQLSessionManager(SessionManager):
    """Session manager backed by MySQL instead of JSONL files."""

    def __init__(
        self,
        workspace,
        *,
        session_store: MySQLSessionStore,
        tenant_key: str | None = None,
    ):
        super().__init__(workspace, tenant_key=tenant_key, remote_only=True)
        self._session_store = session_store

    def _load(self, key: str, tenant_key: str | None = None) -> Session | None:
        tenant = sanitize_tenant_key(tenant_key or self.tenant_key)
        return self._session_store.load(tenant, key)

    def save(self, session: Session, tenant_key: str | None = None) -> None:
        tenant = sanitize_tenant_key(tenant_key or self.tenant_key)
        self._session_store.save(tenant, session)
        self._cache[self._cache_key(session.key, tenant)] = session

    def list_sessions(self, tenant_key: str | None = None) -> list[dict]:
        tenant = None
        if tenant_key and tenant_key != "__default__":
            tenant = sanitize_tenant_key(tenant_key)
        return self._session_store.list_sessions(tenant)

