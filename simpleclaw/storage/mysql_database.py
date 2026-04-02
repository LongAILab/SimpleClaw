"""Core MySQL database wrapper and schema bootstrap."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from .mysql_datetime import _now_dt


def _ensure_tenant_row(db: "MySQLDatabase", tenant_key: str) -> None:
    now_dt = _now_dt()
    db.execute(
        """
        INSERT INTO nb_tenants (tenant_key, status, created_at, updated_at)
        VALUES (%s, 'active', %s, %s)
        ON DUPLICATE KEY UPDATE updated_at=VALUES(updated_at)
        """,
        (tenant_key, now_dt, now_dt),
    )


class MySQLDatabase:
    """Small synchronous PyMySQL wrapper."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        charset: str = "utf8mb4",
        connect_timeout: int = 5,
    ) -> None:
        self._settings = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "database": database,
            "charset": charset,
            "connect_timeout": connect_timeout,
            "autocommit": True,
        }

    @contextmanager
    def connect(self) -> Iterator[Any]:
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - dependency/runtime guard
            raise RuntimeError("MySQL support requires the 'PyMySQL' package") from exc
        conn = pymysql.connect(**self._settings)
        try:
            yield conn
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return int(cur.rowcount or 0)

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [desc[0] for desc in cur.description]
                return dict(zip(cols, row, strict=False))

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                cols = [desc[0] for desc in cur.description]
                return [dict(zip(cols, row, strict=False)) for row in rows]

    def has_table(self, table_name: str) -> bool:
        row = self.fetch_one(
            """
            SELECT 1 AS present
            FROM information_schema.tables
            WHERE table_schema=%s AND table_name=%s
            LIMIT 1
            """,
            (self._settings["database"], table_name),
        )
        return row is not None

    def has_column(self, table_name: str, column_name: str) -> bool:
        row = self.fetch_one(
            """
            SELECT 1 AS present
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s AND column_name=%s
            LIMIT 1
            """,
            (self._settings["database"], table_name, column_name),
        )
        return row is not None

    def has_index(self, table_name: str, index_name: str) -> bool:
        row = self.fetch_one(
            """
            SELECT 1 AS present
            FROM information_schema.statistics
            WHERE table_schema=%s AND table_name=%s AND index_name=%s
            LIMIT 1
            """,
            (self._settings["database"], table_name, index_name),
        )
        return row is not None

    def migrate_ms_column_to_datetime(
        self,
        table_name: str,
        *,
        old_column: str,
        new_column: str,
        nullable: bool,
        old_indexes: tuple[str, ...] = (),
        new_indexes: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if not self.has_table(table_name):
            return
        has_old = self.has_column(table_name, old_column)
        has_new = self.has_column(table_name, new_column)
        if not has_old and has_new:
            for index_name, statement in new_indexes:
                if not self.has_index(table_name, index_name):
                    self.execute(statement)
            return
        if not has_old and not has_new:
            return
        if not has_new:
            self.execute(f"ALTER TABLE {table_name} ADD COLUMN {new_column} DATETIME NULL")
        fallback_expr = "NULL" if nullable else "NOW()"
        self.execute(
            f"""
            UPDATE {table_name}
            SET {new_column}=CASE
                WHEN {old_column} IS NULL OR {old_column} <= 0 THEN {fallback_expr}
                ELSE FROM_UNIXTIME({old_column} / 1000)
            END
            WHERE {new_column} IS NULL
            """
        )
        if not nullable:
            self.execute(f"ALTER TABLE {table_name} MODIFY COLUMN {new_column} DATETIME NOT NULL")
        if has_old:
            for index_name in old_indexes:
                if self.has_index(table_name, index_name):
                    self.execute(f"ALTER TABLE {table_name} DROP INDEX {index_name}")
            self.execute(f"ALTER TABLE {table_name} DROP COLUMN {old_column}")
        for index_name, statement in new_indexes:
            if not self.has_index(table_name, index_name):
                self.execute(statement)

    def ensure_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS nb_tenants (
                tenant_id BIGINT NOT NULL AUTO_INCREMENT,
                tenant_key VARCHAR(255) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (tenant_id),
                UNIQUE KEY uq_tenant_key (tenant_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nb_tenant_state (
                tenant_key VARCHAR(255) PRIMARY KEY,
                primary_session_key VARCHAR(255) NULL,
                primary_channel VARCHAR(255) NULL,
                primary_chat_id VARCHAR(255) NULL,
                last_user_activity_at DATETIME NULL,
                updated_at DATETIME NOT NULL,
                heartbeat_enabled TINYINT(1) NOT NULL,
                heartbeat_interval_s INT NOT NULL,
                heartbeat_next_run_at DATETIME NULL,
                heartbeat_last_run_at DATETIME NULL,
                heartbeat_last_status VARCHAR(32) NULL,
                heartbeat_last_error TEXT NULL,
                last_cron_session_key VARCHAR(255) NULL,
                last_heartbeat_session_key VARCHAR(255) NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nb_cron_jobs (
                job_id VARCHAR(64) PRIMARY KEY,
                tenant_key VARCHAR(255) NULL,
                name VARCHAR(255) NOT NULL,
                enabled TINYINT(1) NOT NULL,
                schedule_json JSON NOT NULL,
                payload_json JSON NOT NULL,
                state_json JSON NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                delete_after_run TINYINT(1) NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nb_sessions (
                tenant_key VARCHAR(255) NOT NULL,
                session_key VARCHAR(255) NOT NULL,
                session_type VARCHAR(32) NOT NULL DEFAULT 'main',
                origin_session_key VARCHAR(255) NULL,
                channel VARCHAR(64) NULL,
                chat_id VARCHAR(255) NULL,
                title VARCHAR(255) NULL,
                is_primary TINYINT(1) NOT NULL DEFAULT 0,
                last_consolidated INT NOT NULL DEFAULT 0,
                metadata_json JSON NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (tenant_key, session_key),
                KEY idx_sessions_tenant_type (tenant_key, session_type),
                KEY idx_sessions_origin (tenant_key, origin_session_key),
                KEY idx_sessions_updated (tenant_key, updated_at)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nb_session_messages (
                tenant_key VARCHAR(255) NOT NULL,
                session_key VARCHAR(255) NOT NULL,
                seq INT NOT NULL,
                content_json JSON NULL,
                message_json JSON NULL,
                role VARCHAR(32) NULL,
                tool_name VARCHAR(128) NULL,
                tool_call_id VARCHAR(128) NULL,
                tokens_estimate INT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (tenant_key, session_key, seq),
                KEY idx_messages_role (tenant_key, session_key, role),
                KEY idx_messages_time (tenant_key, session_key, created_at)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nb_tenant_documents (
                doc_id BIGINT NOT NULL AUTO_INCREMENT,
                tenant_key VARCHAR(255) NOT NULL,
                doc_type VARCHAR(64) NOT NULL,
                doc_name VARCHAR(255) NOT NULL,
                content LONGTEXT NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                format VARCHAR(16) NOT NULL DEFAULT 'markdown',
                version_no INT NOT NULL DEFAULT 1,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                created_by VARCHAR(64) NULL,
                updated_by VARCHAR(64) NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (doc_id),
                UNIQUE KEY uq_tenant_doc_type_name (tenant_key, doc_type, doc_name),
                KEY idx_docs_tenant_type (tenant_key, doc_type),
                KEY idx_docs_active (tenant_key, is_active)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nb_tenant_document_versions (
                version_id BIGINT NOT NULL AUTO_INCREMENT,
                doc_id BIGINT NOT NULL,
                tenant_key VARCHAR(255) NOT NULL,
                doc_type VARCHAR(64) NOT NULL,
                doc_name VARCHAR(255) NOT NULL,
                version_no INT NOT NULL,
                content LONGTEXT NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                change_summary VARCHAR(512) NULL,
                change_source VARCHAR(64) NULL,
                operator_id VARCHAR(255) NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (version_id),
                KEY idx_doc_versions (doc_id, version_no),
                KEY idx_doc_versions_tenant (tenant_key, doc_type, doc_name, version_no)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nb_tenant_memory (
                tenant_key VARCHAR(255) NOT NULL,
                long_term_markdown LONGTEXT NOT NULL,
                structured_facts_json JSON NULL,
                last_updated_session VARCHAR(255) NULL,
                last_consolidation_at DATETIME NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (tenant_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS nb_tenant_memory_events (
                event_id BIGINT NOT NULL AUTO_INCREMENT,
                tenant_key VARCHAR(255) NOT NULL,
                session_key VARCHAR(255) NULL,
                source_type VARCHAR(32) NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                topic VARCHAR(255) NULL,
                keywords_text VARCHAR(1000) NULL,
                importance TINYINT NOT NULL DEFAULT 3,
                history_entry TEXT NOT NULL,
                memory_patch TEXT NULL,
                items_json JSON NULL,
                start_seq INT NULL,
                end_seq INT NULL,
                created_at DATETIME NOT NULL,
                PRIMARY KEY (event_id),
                KEY idx_memory_events_tenant (tenant_key, created_at),
                KEY idx_memory_events_session (tenant_key, session_key),
                KEY idx_memory_events_type (tenant_key, event_type, created_at),
                KEY idx_memory_events_topic (tenant_key, topic),
                KEY idx_memory_events_importance (tenant_key, importance, created_at),
                FULLTEXT KEY ft_memory_events_recall (topic, keywords_text, history_entry)
            )
            """,
        ]
        compatibility_columns = [
            (
                "nb_tenant_state",
                "last_cron_session_key",
                "ALTER TABLE nb_tenant_state ADD COLUMN last_cron_session_key VARCHAR(255) NULL",
            ),
            (
                "nb_tenant_state",
                "last_heartbeat_session_key",
                "ALTER TABLE nb_tenant_state ADD COLUMN last_heartbeat_session_key VARCHAR(255) NULL",
            ),
            (
                "nb_sessions",
                "session_type",
                "ALTER TABLE nb_sessions ADD COLUMN session_type VARCHAR(32) NOT NULL DEFAULT 'main'",
            ),
            (
                "nb_sessions",
                "origin_session_key",
                "ALTER TABLE nb_sessions ADD COLUMN origin_session_key VARCHAR(255) NULL",
            ),
            (
                "nb_sessions",
                "channel",
                "ALTER TABLE nb_sessions ADD COLUMN channel VARCHAR(64) NULL",
            ),
            (
                "nb_sessions",
                "chat_id",
                "ALTER TABLE nb_sessions ADD COLUMN chat_id VARCHAR(255) NULL",
            ),
            (
                "nb_sessions",
                "title",
                "ALTER TABLE nb_sessions ADD COLUMN title VARCHAR(255) NULL",
            ),
            (
                "nb_sessions",
                "is_primary",
                "ALTER TABLE nb_sessions ADD COLUMN is_primary TINYINT(1) NOT NULL DEFAULT 0",
            ),
            (
                "nb_session_messages",
                "content_json",
                "ALTER TABLE nb_session_messages ADD COLUMN content_json JSON NULL",
            ),
            (
                "nb_session_messages",
                "message_json",
                "ALTER TABLE nb_session_messages ADD COLUMN message_json JSON NULL",
            ),
            (
                "nb_session_messages",
                "role",
                "ALTER TABLE nb_session_messages ADD COLUMN role VARCHAR(32) NULL",
            ),
            (
                "nb_session_messages",
                "tool_name",
                "ALTER TABLE nb_session_messages ADD COLUMN tool_name VARCHAR(128) NULL",
            ),
            (
                "nb_session_messages",
                "tool_call_id",
                "ALTER TABLE nb_session_messages ADD COLUMN tool_call_id VARCHAR(128) NULL",
            ),
            (
                "nb_session_messages",
                "tokens_estimate",
                "ALTER TABLE nb_session_messages ADD COLUMN tokens_estimate INT NULL",
            ),
            (
                "nb_session_messages",
                "created_at",
                "ALTER TABLE nb_session_messages ADD COLUMN created_at DATETIME NULL",
            ),
        ]
        for statement in statements:
            self.execute(statement)
        for table_name, column_name, statement in compatibility_columns:
            if not self.has_column(table_name, column_name):
                self.execute(statement)
        self.migrate_ms_column_to_datetime("nb_tenants", old_column="created_at_ms", new_column="created_at", nullable=False)
        self.migrate_ms_column_to_datetime("nb_tenants", old_column="updated_at_ms", new_column="updated_at", nullable=False)
        self.migrate_ms_column_to_datetime("nb_tenant_state", old_column="last_user_activity_at_ms", new_column="last_user_activity_at", nullable=True)
        self.migrate_ms_column_to_datetime("nb_tenant_state", old_column="updated_at_ms", new_column="updated_at", nullable=False)
        self.migrate_ms_column_to_datetime("nb_tenant_state", old_column="heartbeat_next_run_at_ms", new_column="heartbeat_next_run_at", nullable=True)
        self.migrate_ms_column_to_datetime("nb_tenant_state", old_column="heartbeat_last_run_at_ms", new_column="heartbeat_last_run_at", nullable=True)
        self.migrate_ms_column_to_datetime("nb_cron_jobs", old_column="created_at_ms", new_column="created_at", nullable=False)
        self.migrate_ms_column_to_datetime("nb_cron_jobs", old_column="updated_at_ms", new_column="updated_at", nullable=False)
        self.migrate_ms_column_to_datetime(
            "nb_session_messages",
            old_column="created_at_ms",
            new_column="created_at",
            nullable=False,
            old_indexes=("idx_messages_time",),
            new_indexes=(
                ("idx_messages_time", "CREATE INDEX idx_messages_time ON nb_session_messages (tenant_key, session_key, created_at)"),
            ),
        )
        self.migrate_ms_column_to_datetime("nb_tenant_documents", old_column="created_at_ms", new_column="created_at", nullable=False)
        self.migrate_ms_column_to_datetime("nb_tenant_documents", old_column="updated_at_ms", new_column="updated_at", nullable=False)
        self.migrate_ms_column_to_datetime("nb_tenant_document_versions", old_column="created_at_ms", new_column="created_at", nullable=False)
        self.migrate_ms_column_to_datetime("nb_tenant_memory", old_column="last_consolidation_ms", new_column="last_consolidation_at", nullable=True)
        self.migrate_ms_column_to_datetime("nb_tenant_memory", old_column="updated_at_ms", new_column="updated_at", nullable=False)
        self.migrate_ms_column_to_datetime(
            "nb_tenant_memory_events",
            old_column="created_at_ms",
            new_column="created_at",
            nullable=False,
            old_indexes=("idx_memory_events_tenant", "idx_memory_events_type", "idx_memory_events_importance"),
            new_indexes=(
                ("idx_memory_events_tenant", "CREATE INDEX idx_memory_events_tenant ON nb_tenant_memory_events (tenant_key, created_at)"),
                ("idx_memory_events_type", "CREATE INDEX idx_memory_events_type ON nb_tenant_memory_events (tenant_key, event_type, created_at)"),
                ("idx_memory_events_importance", "CREATE INDEX idx_memory_events_importance ON nb_tenant_memory_events (tenant_key, importance, created_at)"),
            ),
        )
