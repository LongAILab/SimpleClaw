"""MySQL-backed session store."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from simpleclaw.session.manager import Session

from .mysql_database import MySQLDatabase, _ensure_tenant_row
from .mysql_datetime import _ms_to_dt, _now_ms


class MySQLSessionStore:
    """MySQL persistence for session metadata and append-only messages."""

    def __init__(self, db: MySQLDatabase) -> None:
        self._db = db
        self._table_cache: dict[str, bool] = {}
        self._column_cache: dict[tuple[str, str], bool] = {}

    def _has_table(self, table_name: str) -> bool:
        if table_name not in self._table_cache:
            self._table_cache[table_name] = self._db.has_table(table_name)
        return self._table_cache[table_name]

    def _has_column(self, table_name: str, column_name: str) -> bool:
        key = (table_name, column_name)
        if key not in self._column_cache:
            self._column_cache[key] = self._db.has_column(table_name, column_name)
        return self._column_cache[key]

    def _session_table(self) -> str:
        return "nb_sessions" if self._has_table("nb_sessions") else "nb_session_meta"

    def _message_json_column(self) -> str:
        if self._has_column("nb_session_messages", "content_json"):
            return "content_json"
        return "message_json"

    @staticmethod
    def _decode_json(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        return json.loads(value)

    @staticmethod
    def _as_datetime(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    @staticmethod
    def _message_created_at_ms(message: dict[str, Any]) -> int:
        timestamp = message.get("timestamp")
        if isinstance(timestamp, str):
            try:
                return int(datetime.fromisoformat(timestamp).timestamp() * 1000)
            except ValueError:
                pass
        return _now_ms()

    @classmethod
    def _message_created_at(cls, message: dict[str, Any]) -> datetime:
        return _ms_to_dt(cls._message_created_at_ms(message))

    def _load_persisted_session_row(self, tenant_key: str, session_key: str) -> dict[str, Any] | None:
        if self._session_table() != "nb_sessions":
            return None
        return self._db.fetch_one(
            """
            SELECT session_type, origin_session_key, channel, chat_id, title, is_primary
            FROM nb_sessions
            WHERE tenant_key=%s AND session_key=%s
            """,
            (tenant_key, session_key),
        )

    @staticmethod
    def _merge_metadata_from_row(
        metadata: dict[str, Any],
        row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = dict(metadata)
        if not row:
            return merged
        session_type = row.get("session_type")
        if session_type:
            merged["session_type"] = str(session_type)
        else:
            merged.setdefault("session_type", "main")
        origin_session_key = row.get("origin_session_key")
        if origin_session_key:
            merged["origin_session_key"] = str(origin_session_key)
        elif merged.get("session_type") == "main":
            merged.pop("origin_session_key", None)
        for field in ("channel", "chat_id", "title"):
            value = row.get(field)
            if value is not None:
                merged[field] = value
        if row.get("is_primary") is not None:
            merged["is_primary"] = bool(row.get("is_primary"))
        return merged

    def _resolve_session_metadata(self, tenant_key: str, session: Session) -> dict[str, Any]:
        persisted = self._load_persisted_session_row(tenant_key, session.key)
        metadata = self._merge_metadata_from_row(session.metadata, persisted)
        metadata["session_type"] = str(metadata.get("session_type") or "main")
        if metadata["session_type"] == "main":
            metadata.pop("origin_session_key", None)
        elif metadata.get("origin_session_key") is not None:
            metadata["origin_session_key"] = str(metadata["origin_session_key"])
        if metadata.get("channel") is not None:
            metadata["channel"] = str(metadata["channel"])
        if metadata.get("chat_id") is not None:
            metadata["chat_id"] = str(metadata["chat_id"])
        if "is_primary" in metadata:
            metadata["is_primary"] = bool(metadata["is_primary"])
        session.metadata = metadata
        return metadata

    def load(self, tenant_key: str, session_key: str) -> Session | None:
        table = self._session_table()
        meta = self._db.fetch_one(
            f"SELECT * FROM {table} WHERE tenant_key=%s AND session_key=%s",
            (tenant_key, session_key),
        )
        if meta is None and table == "nb_sessions" and self._has_table("nb_session_meta"):
            meta = self._db.fetch_one(
                "SELECT * FROM nb_session_meta WHERE tenant_key=%s AND session_key=%s",
                (tenant_key, session_key),
            )
            table = "nb_session_meta"
        if meta is None:
            return None
        message_column = self._message_json_column()
        message_rows = self._db.fetch_all(
            """
            SELECT {} AS payload FROM nb_session_messages
            WHERE tenant_key=%s AND session_key=%s
            ORDER BY seq ASC
            """.format(message_column),
            (tenant_key, session_key),
        )
        metadata = self._decode_json(meta.get("metadata_json")) or {}
        if table == "nb_sessions":
            metadata = self._merge_metadata_from_row(metadata, meta)
        return Session(
            key=session_key,
            messages=[self._decode_json(row["payload"]) for row in message_rows],
            created_at=self._as_datetime(meta["created_at"]),
            updated_at=self._as_datetime(meta["updated_at"]),
            metadata=metadata,
            last_consolidated=int(meta["last_consolidated"]),
        )

    def save(self, tenant_key: str, session: Session) -> None:
        _ensure_tenant_row(self._db, tenant_key)
        metadata = self._resolve_session_metadata(tenant_key, session)
        metadata_json = json.dumps(metadata, ensure_ascii=False)
        if self._session_table() == "nb_sessions":
            self._db.execute(
                """
                INSERT INTO nb_sessions (
                    tenant_key, session_key, session_type, origin_session_key,
                    channel, chat_id, title, is_primary, last_consolidated,
                    metadata_json, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    session_type=VALUES(session_type),
                    origin_session_key=VALUES(origin_session_key),
                    channel=VALUES(channel),
                    chat_id=VALUES(chat_id),
                    title=VALUES(title),
                    is_primary=VALUES(is_primary),
                    last_consolidated=VALUES(last_consolidated),
                    metadata_json=VALUES(metadata_json),
                    created_at=VALUES(created_at),
                    updated_at=VALUES(updated_at)
                """,
                (
                    tenant_key,
                    session.key,
                    str(metadata.get("session_type") or "main"),
                    metadata.get("origin_session_key"),
                    metadata.get("channel"),
                    metadata.get("chat_id"),
                    metadata.get("title"),
                    1 if metadata.get("is_primary") else 0,
                    session.last_consolidated,
                    metadata_json,
                    session.created_at,
                    session.updated_at,
                ),
            )
        else:
            self._db.execute(
                """
                INSERT INTO nb_session_meta (
                    tenant_key, session_key, created_at, updated_at, metadata_json, last_consolidated
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    created_at=VALUES(created_at),
                    updated_at=VALUES(updated_at),
                    metadata_json=VALUES(metadata_json),
                    last_consolidated=VALUES(last_consolidated)
                """,
                (
                    tenant_key,
                    session.key,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    metadata_json,
                    session.last_consolidated,
                ),
            )
        self._db.execute(
            "DELETE FROM nb_session_messages WHERE tenant_key=%s AND session_key=%s",
            (tenant_key, session.key),
        )
        has_content_json = self._has_column("nb_session_messages", "content_json")
        has_message_json = self._has_column("nb_session_messages", "message_json")
        has_role = self._has_column("nb_session_messages", "role")
        has_tool_name = self._has_column("nb_session_messages", "tool_name")
        has_tool_call_id = self._has_column("nb_session_messages", "tool_call_id")
        has_tokens_estimate = self._has_column("nb_session_messages", "tokens_estimate")
        has_created_at = self._has_column("nb_session_messages", "created_at")
        has_created_at_ms = self._has_column("nb_session_messages", "created_at_ms")
        for seq, message in enumerate(session.messages):
            payload = json.dumps(message, ensure_ascii=False)
            columns = ["tenant_key", "session_key", "seq"]
            values: list[Any] = [tenant_key, session.key, seq]
            if has_content_json:
                columns.append("content_json")
                values.append(payload)
            if has_message_json:
                columns.append("message_json")
                values.append(payload)
            if has_role:
                columns.append("role")
                values.append(str(message.get("role") or "assistant"))
            if has_tool_name:
                columns.append("tool_name")
                values.append(message.get("name") or message.get("tool_name"))
            if has_tool_call_id:
                columns.append("tool_call_id")
                values.append(message.get("tool_call_id"))
            if has_tokens_estimate:
                token_estimate = message.get("tokens_estimate")
                columns.append("tokens_estimate")
                values.append(int(token_estimate) if isinstance(token_estimate, int) else None)
            if has_created_at:
                columns.append("created_at")
                values.append(self._message_created_at(message))
            elif has_created_at_ms:
                columns.append("created_at_ms")
                values.append(self._message_created_at_ms(message))
            placeholders = ", ".join(["%s"] * len(columns))
            self._db.execute(
                f"INSERT INTO nb_session_messages ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(values),
            )

    def list_sessions(self, tenant_key: str | None = None) -> list[dict[str, Any]]:
        table = self._session_table()
        if tenant_key:
            rows = self._db.fetch_all(
                """
                SELECT tenant_key, session_key, created_at, updated_at
                FROM {}
                WHERE tenant_key=%s
                ORDER BY updated_at DESC
                """.format(table),
                (tenant_key,),
            )
        else:
            rows = self._db.fetch_all(
                """
                SELECT tenant_key, session_key, created_at, updated_at
                FROM {}
                ORDER BY updated_at DESC
                """.format(table)
            )
        return [
            {
                "tenant_key": row["tenant_key"],
                "key": row["session_key"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "path": f"mysql://{table}",
            }
            for row in rows
        ]
