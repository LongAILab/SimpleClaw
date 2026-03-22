"""MySQL-backed tenant document store."""

from __future__ import annotations

import hashlib
from typing import Any

from .mysql_database import MySQLDatabase, _ensure_tenant_row
from .mysql_datetime import _now_dt

_LEGACY_PLACEHOLDER_BY_DOC: dict[str, str] = {
    "SOUL.md": (
        "# Tenant Soul Override\n\n"
        "Use this file for tenant-specific assistant personality additions.\n"
        "It is appended after the shared base `SOUL.md`.\n\n"
        "Recommended content:\n"
        "- desired tone and relationship style\n"
        "- how warm, playful, or direct the assistant should be\n"
        "- tenant-specific companionship style\n\n"
        "Do not put dynamic user memory or task lists here.\n"
    ),
    "USER.md": (
        "# Tenant User Override\n\n"
        "Use this file for stable tenant-specific user profile defaults.\n"
        "It is appended after the shared base `USER.md`.\n\n"
        "Recommended content:\n"
        "- preferred name or addressing style\n"
        "- stable communication preferences\n"
        "- known boundaries or long-term support preferences\n\n"
        "Do not put temporary conversation details here; those belong in memory files.\n"
    ),
    "HEARTBEAT.md": (
        "# Tenant Heartbeat Override\n\n"
        "Use this file for tenant-specific periodic companionship and follow-up guidance.\n"
        "It is appended after the shared base `HEARTBEAT.md` when heartbeat runs.\n\n"
        "Recommended content:\n"
        "- recurring check-in style\n"
        "- topics worth revisiting later\n"
        "- tenant-specific proactive support rules\n\n"
        "Do not use this file for normal chat prompt rules.\n"
    ),
}


class MySQLTenantDocumentStore:
    """Current tenant prompt documents and version history."""

    def __init__(self, db: MySQLDatabase) -> None:
        self._db = db

    def _ensure_tenant(self, tenant_key: str) -> None:
        _ensure_tenant_row(self._db, tenant_key)

    @staticmethod
    def _hash_content(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_legacy_placeholder(doc_name: str, content: str) -> bool:
        template = _LEGACY_PLACEHOLDER_BY_DOC.get(doc_name)
        return bool(template) and content.strip() == template.strip()

    def get_active_document(self, tenant_key: str, doc_type: str, doc_name: str) -> dict[str, Any] | None:
        return self._db.fetch_one(
            """
            SELECT *
            FROM nb_tenant_documents
            WHERE tenant_key=%s AND doc_type=%s AND doc_name=%s AND is_active=1
            LIMIT 1
            """,
            (tenant_key, doc_type, doc_name),
        )

    def get_active_content(self, tenant_key: str, doc_type: str, doc_name: str) -> str | None:
        row = self.get_active_document(tenant_key, doc_type, doc_name)
        if row is None or row.get("content") is None:
            return None
        content = str(row["content"])
        return None if self._is_legacy_placeholder(doc_name, content) else content

    def upsert_document(
        self,
        *,
        tenant_key: str,
        doc_type: str,
        doc_name: str,
        content: str,
        created_by: str | None = None,
        updated_by: str | None = None,
        is_active: bool = True,
        change_summary: str | None = None,
        change_source: str | None = None,
        operator_id: str | None = None,
    ) -> int:
        self._ensure_tenant(tenant_key)
        now_dt = _now_dt()
        content_hash = self._hash_content(content)
        existing = self.get_active_document(tenant_key, doc_type, doc_name)
        if existing is not None and existing.get("content_hash") == content_hash and bool(existing.get("is_active", 1)) == is_active:
            self._db.execute(
                """
                UPDATE nb_tenant_documents
                SET updated_at=%s, updated_by=%s
                WHERE doc_id=%s
                """,
                (now_dt, updated_by or created_by, existing["doc_id"]),
            )
            return int(existing["doc_id"])

        if existing is None:
            self._db.execute(
                """
                INSERT INTO nb_tenant_documents (
                    tenant_key, doc_type, doc_name, content, content_hash, format,
                    version_no, is_active, created_by, updated_by, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, 'markdown', 1, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_key,
                    doc_type,
                    doc_name,
                    content,
                    content_hash,
                    1 if is_active else 0,
                    created_by,
                    updated_by or created_by,
                    now_dt,
                    now_dt,
                ),
            )
            row = self.get_active_document(tenant_key, doc_type, doc_name)
            if row is None:
                raise RuntimeError("Failed to read inserted tenant document")
            doc_id = int(row["doc_id"])
            version_no = 1
        else:
            doc_id = int(existing["doc_id"])
            version_no = int(existing.get("version_no") or 1) + 1
            self._db.execute(
                """
                UPDATE nb_tenant_documents
                SET content=%s, content_hash=%s, version_no=%s, is_active=%s,
                    updated_by=%s, updated_at=%s
                WHERE doc_id=%s
                """,
                (
                    content,
                    content_hash,
                    version_no,
                    1 if is_active else 0,
                    updated_by or created_by,
                    now_dt,
                    doc_id,
                ),
            )

        self._db.execute(
            """
            INSERT INTO nb_tenant_document_versions (
                doc_id, tenant_key, doc_type, doc_name, version_no, content, content_hash,
                change_summary, change_source, operator_id, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                doc_id,
                tenant_key,
                doc_type,
                doc_name,
                version_no,
                content,
                content_hash,
                change_summary,
                change_source or updated_by or created_by,
                operator_id,
                now_dt,
            ),
        )
        return doc_id

    def seed_default_documents(self, tenant_key: str) -> None:
        """Do not seed placeholder tenant override documents."""
        return
