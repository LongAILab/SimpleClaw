"""Tenant bootstrap helpers for MySQL-backed state."""

from __future__ import annotations

from .mysql_database import MySQLDatabase, _ensure_tenant_row
from .mysql_tenant_documents import MySQLTenantDocumentStore
from .mysql_tenant_memory import MySQLTenantMemoryStore


class MySQLTenantProvisioner:
    """Provision one tenant's default database-backed state eagerly."""

    def __init__(
        self,
        db: MySQLDatabase,
        *,
        document_store: MySQLTenantDocumentStore,
        memory_store: MySQLTenantMemoryStore,
    ) -> None:
        self._db = db
        self._document_store = document_store
        self._memory_store = memory_store

    def ensure_initialized(self, tenant_key: str | None) -> str:
        tenant = (tenant_key or "__default__").strip() or "__default__"
        _ensure_tenant_row(self._db, tenant)
        self._document_store.seed_default_documents(tenant)
        self._memory_store.seed_default_snapshot(tenant)
        return tenant
