"""MySQL-backed tenant state repository."""

from __future__ import annotations

from typing import Any

from simpleclaw.tenant.state import TenantHeartbeatState, TenantState

from .mysql_database import MySQLDatabase, _ensure_tenant_row
from .mysql_datetime import _dt_to_ms, _ms_to_dt, _now_ms


class MySQLTenantStateRepository:
    """MySQL-backed tenant state repository."""

    def __init__(
        self,
        db: MySQLDatabase,
        *,
        default_heartbeat_enabled: bool = True,
        default_heartbeat_interval_s: int = 30 * 60,
        default_heartbeat_stagger_s: int = 0,
    ) -> None:
        from simpleclaw.tenant.state import TenantStateRepository

        self._db = db
        self._defaults = TenantStateRepository(
            default_heartbeat_enabled=default_heartbeat_enabled,
            default_heartbeat_interval_s=default_heartbeat_interval_s,
            default_heartbeat_stagger_s=default_heartbeat_stagger_s,
        )

    def _ensure_tenant(self, tenant_key: str) -> None:
        _ensure_tenant_row(self._db, tenant_key)

    def _new_state(self, tenant_key: str | None) -> TenantState:
        return self._defaults._new_state(tenant_key)

    def _to_row(self, state: TenantState) -> tuple[Any, ...]:
        return (
            state.tenant_key,
            state.primary_session_key,
            state.primary_channel,
            state.primary_chat_id,
            _ms_to_dt(state.last_user_activity_at_ms, nullable=True),
            _ms_to_dt(state.updated_at_ms),
            1 if state.heartbeat.enabled else 0,
            state.heartbeat.interval_s,
            _ms_to_dt(state.heartbeat.next_run_at_ms, nullable=True),
            _ms_to_dt(state.heartbeat.last_run_at_ms, nullable=True),
            state.heartbeat.last_status,
            state.heartbeat.last_error,
        )

    @staticmethod
    def _from_row(row: dict[str, Any]) -> TenantState:
        return TenantState(
            tenant_key=str(row["tenant_key"]),
            primary_session_key=row.get("primary_session_key"),
            primary_channel=row.get("primary_channel"),
            primary_chat_id=row.get("primary_chat_id"),
            last_user_activity_at_ms=_dt_to_ms(row.get("last_user_activity_at") or row.get("last_user_activity_at_ms")),
            updated_at_ms=int(_dt_to_ms(row.get("updated_at") or row.get("updated_at_ms")) or _now_ms()),
            heartbeat=TenantHeartbeatState(
                enabled=bool(row.get("heartbeat_enabled")),
                interval_s=int(row.get("heartbeat_interval_s") or 30 * 60),
                next_run_at_ms=_dt_to_ms(row.get("heartbeat_next_run_at") or row.get("heartbeat_next_run_at_ms")),
                last_run_at_ms=_dt_to_ms(row.get("heartbeat_last_run_at") or row.get("heartbeat_last_run_at_ms")),
                last_status=row.get("heartbeat_last_status"),
                last_error=row.get("heartbeat_last_error"),
            ),
        )

    def get(self, tenant_key: str | None) -> TenantState | None:
        tenant = (tenant_key or "__default__").strip() or "__default__"
        row = self._db.fetch_one(
            "SELECT * FROM nb_tenant_state WHERE tenant_key=%s",
            (tenant,),
        )
        return self._from_row(row) if row else None

    def get_or_create(self, tenant_key: str | None) -> TenantState:
        state = self.get(tenant_key)
        if state is not None:
            return state
        state = self._new_state(tenant_key)
        self._ensure_tenant(state.tenant_key)
        self.save(state)
        return state

    def save(self, state: TenantState) -> TenantState:
        self._ensure_tenant(state.tenant_key)
        state.updated_at_ms = _now_ms()
        self._db.execute(
            """
            INSERT INTO nb_tenant_state (
                tenant_key, primary_session_key, primary_channel, primary_chat_id,
                last_user_activity_at, updated_at, heartbeat_enabled,
                heartbeat_interval_s, heartbeat_next_run_at, heartbeat_last_run_at,
                heartbeat_last_status, heartbeat_last_error
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                primary_session_key=VALUES(primary_session_key),
                primary_channel=VALUES(primary_channel),
                primary_chat_id=VALUES(primary_chat_id),
                last_user_activity_at=VALUES(last_user_activity_at),
                updated_at=VALUES(updated_at),
                heartbeat_enabled=VALUES(heartbeat_enabled),
                heartbeat_interval_s=VALUES(heartbeat_interval_s),
                heartbeat_next_run_at=VALUES(heartbeat_next_run_at),
                heartbeat_last_run_at=VALUES(heartbeat_last_run_at),
                heartbeat_last_status=VALUES(heartbeat_last_status),
                heartbeat_last_error=VALUES(heartbeat_last_error)
            """,
            self._to_row(state),
        )
        return state

    def touch_interaction(
        self,
        *,
        tenant_key: str | None,
        session_key: str,
        channel: str,
        chat_id: str,
        activity_at_ms: int | None = None,
    ) -> TenantState:
        state = self.get_or_create(tenant_key)
        now_ms = activity_at_ms or _now_ms()
        state.primary_session_key = session_key
        state.primary_channel = channel
        state.primary_chat_id = chat_id
        state.last_user_activity_at_ms = now_ms
        if state.heartbeat.next_run_at_ms is None:
            state.heartbeat.next_run_at_ms = now_ms + state.heartbeat.interval_s * 1000
        return self.save(state)

    def configure_heartbeat(
        self,
        tenant_key: str | None,
        *,
        enabled: bool | None = None,
        interval_s: int | None = None,
        next_run_at_ms: int | None = None,
    ) -> TenantState:
        state = self.get_or_create(tenant_key)
        if enabled is not None:
            state.heartbeat.enabled = enabled
        if interval_s is not None:
            state.heartbeat.interval_s = max(1, interval_s)
        if next_run_at_ms is not None:
            state.heartbeat.next_run_at_ms = next_run_at_ms
        elif state.heartbeat.next_run_at_ms is None:
            state.heartbeat.next_run_at_ms = _now_ms() + state.heartbeat.interval_s * 1000
        return self.save(state)

    def mark_heartbeat_result(
        self,
        tenant_key: str | None,
        *,
        status: str,
        next_run_at_ms: int,
        error: str | None = None,
        ran_at_ms: int | None = None,
    ) -> TenantState:
        state = self.get_or_create(tenant_key)
        now_ms = ran_at_ms or _now_ms()
        state.heartbeat.last_run_at_ms = now_ms
        state.heartbeat.last_status = status
        state.heartbeat.last_error = error
        state.heartbeat.next_run_at_ms = next_run_at_ms
        return self.save(state)

    def list_states(self) -> list[TenantState]:
        rows = self._db.fetch_all("SELECT * FROM nb_tenant_state ORDER BY updated_at DESC")
        return [self._from_row(row) for row in rows]

    def list_due_heartbeat_tenants(
        self,
        *,
        now_ms: int | None = None,
        tenant_filter: str | None = None,
    ) -> list[TenantState]:
        cutoff = now_ms or _now_ms()
        cutoff_dt = _ms_to_dt(cutoff)
        if tenant_filter:
            rows = self._db.fetch_all(
                """
                SELECT * FROM nb_tenant_state
                WHERE tenant_key=%s
                  AND heartbeat_enabled=1
                  AND heartbeat_next_run_at IS NOT NULL
                  AND heartbeat_next_run_at<=%s
                ORDER BY updated_at DESC
                """,
                (tenant_filter, cutoff_dt),
            )
        else:
            rows = self._db.fetch_all(
                """
                SELECT * FROM nb_tenant_state
                WHERE heartbeat_enabled=1
                  AND heartbeat_next_run_at IS NOT NULL
                  AND heartbeat_next_run_at<=%s
                ORDER BY updated_at DESC
                """,
                (cutoff_dt,),
            )
        return [self._from_row(row) for row in rows]
