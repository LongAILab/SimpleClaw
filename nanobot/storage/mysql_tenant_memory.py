"""MySQL-backed tenant memory store."""

from __future__ import annotations

import json
from typing import Any

from nanobot.utils.helpers import get_default_tenant_memory_markdown

from .mysql_database import MySQLDatabase, _ensure_tenant_row
from .mysql_datetime import _ms_to_dt, _now_dt


class MySQLTenantMemoryStore:
    """Tenant long-term memory snapshot and recall events."""

    _LEGACY_MEMORY_HEADER = "# Long-term Memory"
    _LEGACY_MEMORY_INTRO = "This file stores important information that should persist across sessions."
    _LEGACY_MEMORY_FOOTER = "*This file is automatically updated by nanobot when important information should be remembered.*"
    _LEGACY_SECTION_PLACEHOLDERS: dict[str, str] = {
        "## User Information": "(Important facts about the user)",
        "## Preferences": "(User preferences learned over time)",
        "## Project Context": "(Information about ongoing projects)",
        "## Important Notes": "(Things to remember)",
    }

    def __init__(self, db: MySQLDatabase) -> None:
        self._db = db

    def _ensure_tenant(self, tenant_key: str) -> None:
        _ensure_tenant_row(self._db, tenant_key)

    def load_snapshot(self, tenant_key: str) -> dict[str, Any] | None:
        row = self._db.fetch_one("SELECT * FROM nb_tenant_memory WHERE tenant_key=%s", (tenant_key,))
        if row and isinstance(row.get("structured_facts_json"), str):
            row["structured_facts_json"] = json.loads(row["structured_facts_json"])
        return row

    @classmethod
    def _clean_legacy_long_term(cls, content: str) -> str:
        """Remove old template scaffolding while keeping real remembered facts."""
        text = content.replace("\r\n", "\n").strip()
        if not text:
            return ""

        template = get_default_tenant_memory_markdown().replace("\r\n", "\n").strip()
        if template and text == template:
            return ""

        lines = text.split("\n")
        cleaned: list[str] = []
        changed = False
        i = 0

        while i < len(lines):
            raw_line = lines[i].rstrip()
            line = raw_line.strip()

            if not line:
                i += 1
                continue
            if line in {
                cls._LEGACY_MEMORY_HEADER,
                cls._LEGACY_MEMORY_INTRO,
                "---",
                cls._LEGACY_MEMORY_FOOTER,
            }:
                changed = True
                i += 1
                continue
            if line in cls._LEGACY_SECTION_PLACEHOLDERS:
                header = line
                placeholder = cls._LEGACY_SECTION_PLACEHOLDERS[header]
                changed = True
                i += 1
                block: list[str] = []
                while i < len(lines):
                    next_line = lines[i].rstrip()
                    next_stripped = next_line.strip()
                    if next_stripped in cls._LEGACY_SECTION_PLACEHOLDERS:
                        break
                    if next_stripped in {
                        cls._LEGACY_MEMORY_HEADER,
                        cls._LEGACY_MEMORY_INTRO,
                        "---",
                        cls._LEGACY_MEMORY_FOOTER,
                    }:
                        changed = True
                        i += 1
                        continue
                    if next_stripped and next_stripped != placeholder:
                        block.append(next_line)
                    elif next_stripped == placeholder:
                        changed = True
                    i += 1
                while block and not block[0].strip():
                    block.pop(0)
                while block and not block[-1].strip():
                    block.pop()
                if block:
                    if cleaned:
                        cleaned.append("")
                    cleaned.append(header)
                    cleaned.extend(block)
                continue
            cleaned.append(raw_line)
            i += 1

        result = "\n".join(cleaned).strip()
        return result if changed else text

    def read_long_term(self, tenant_key: str) -> str:
        snapshot = self.load_snapshot(tenant_key)
        if snapshot is None:
            return ""
        content = str(snapshot.get("long_term_markdown") or "")
        return self._clean_legacy_long_term(content)

    def save_snapshot(
        self,
        tenant_key: str,
        *,
        long_term_markdown: str,
        structured_facts_json: dict[str, Any] | None = None,
        last_updated_session: str | None = None,
        last_consolidation_ms: int | None = None,
    ) -> None:
        self._ensure_tenant(tenant_key)
        now_dt = _now_dt()
        self._db.execute(
            """
            INSERT INTO nb_tenant_memory (
                tenant_key, long_term_markdown, structured_facts_json,
                last_updated_session, last_consolidation_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                long_term_markdown=VALUES(long_term_markdown),
                structured_facts_json=COALESCE(VALUES(structured_facts_json), structured_facts_json),
                last_updated_session=COALESCE(VALUES(last_updated_session), last_updated_session),
                last_consolidation_at=COALESCE(VALUES(last_consolidation_at), last_consolidation_at),
                updated_at=VALUES(updated_at)
            """,
            (
                tenant_key,
                long_term_markdown,
                json.dumps(structured_facts_json, ensure_ascii=False) if structured_facts_json is not None else None,
                last_updated_session,
                _ms_to_dt(last_consolidation_ms, nullable=True),
                now_dt,
            ),
        )

    def append_event(
        self,
        tenant_key: str,
        *,
        history_entry: str,
        session_key: str | None = None,
        source_type: str = "manual",
        event_type: str = "conversation_recap",
        topic: str | None = None,
        keywords_text: str | None = None,
        importance: int = 3,
        memory_patch: str | None = None,
        items_json: Any = None,
        start_seq: int | None = None,
        end_seq: int | None = None,
    ) -> None:
        self._ensure_tenant(tenant_key)
        self._db.execute(
            """
            INSERT INTO nb_tenant_memory_events (
                tenant_key, session_key, source_type, event_type, topic, keywords_text,
                importance, history_entry, memory_patch, items_json, start_seq, end_seq, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_key,
                session_key,
                source_type,
                event_type,
                topic,
                keywords_text,
                max(1, min(5, importance)),
                history_entry,
                memory_patch,
                json.dumps(items_json, ensure_ascii=False) if items_json is not None else None,
                start_seq,
                end_seq,
                _now_dt(),
            ),
        )

    def render_history_text(self, tenant_key: str, *, limit: int = 200) -> str:
        rows = self._db.fetch_all(
            """
            SELECT history_entry
            FROM nb_tenant_memory_events
            WHERE tenant_key=%s
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (tenant_key, limit),
        )
        return "\n\n".join(str(row.get("history_entry") or "").strip() for row in rows if row.get("history_entry"))

    def seed_default_snapshot(self, tenant_key: str) -> None:
        """Do not seed placeholder memory for MySQL-backed tenants."""
        return

    def overwrite_history(self, tenant_key: str, content: str) -> None:
        """Replace the rendered history archive with user-provided text blocks."""
        self._ensure_tenant(tenant_key)
        self._db.execute("DELETE FROM nb_tenant_memory_events WHERE tenant_key=%s", (tenant_key,))
        blocks = [block.strip() for block in content.replace("\r\n", "\n").split("\n\n") if block.strip()]
        for block in blocks:
            self.append_event(
                tenant_key,
                history_entry=block,
                source_type="manual",
                event_type="manual_edit",
            )
