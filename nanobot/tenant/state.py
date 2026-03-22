"""Tenant-scoped runtime state repository."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

from nanobot.config.paths import get_runtime_subdir, get_tenant_runtime_subdir, sanitize_tenant_key


def _now_ms() -> int:
    return int(time.time() * 1000)


@contextmanager
def _locked_file(path: Path) -> Iterator[tuple[Path, object]]:
    """Yield an exclusively locked file handle for cross-process coordination."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        yield path, f
        f.flush()
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@dataclass
class TenantHeartbeatState:
    """Tenant-level heartbeat scheduling state."""

    enabled: bool = True
    interval_s: int = 30 * 60
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "skip", "defer", "error", "queued"] | None = None
    last_error: str | None = None


@dataclass
class TenantState:
    """Persistent tenant runtime state."""

    tenant_key: str
    primary_session_key: str | None = None
    primary_channel: str | None = None
    primary_chat_id: str | None = None
    last_user_activity_at_ms: int | None = None
    updated_at_ms: int = field(default_factory=_now_ms)
    heartbeat: TenantHeartbeatState = field(default_factory=TenantHeartbeatState)


class TenantStateRepository:
    """File-backed repository for tenant runtime state."""

    def __init__(
        self,
        *,
        runtime_root: Path | None = None,
        default_heartbeat_enabled: bool = True,
        default_heartbeat_interval_s: int = 30 * 60,
        default_heartbeat_stagger_s: int = 0,
    ) -> None:
        self._runtime_root = runtime_root
        self._default_heartbeat_enabled = default_heartbeat_enabled
        self._default_heartbeat_interval_s = max(1, default_heartbeat_interval_s)
        self._default_heartbeat_stagger_s = max(0, default_heartbeat_stagger_s)

    def _runtime_dir(self, name: str) -> Path:
        if self._runtime_root is not None:
            path = self._runtime_root / name
            path.mkdir(parents=True, exist_ok=True)
            return path
        return get_runtime_subdir(name)

    def _state_path(self, tenant_key: str | None) -> Path:
        tenant = sanitize_tenant_key(tenant_key)
        if self._runtime_root is not None:
            path = self._runtime_dir("tenants") / tenant / "state"
            path.mkdir(parents=True, exist_ok=True)
            return path / "tenant_state.json"
        return get_tenant_runtime_subdir("state", tenant) / "tenant_state.json"

    def _tenants_root(self) -> Path:
        return self._runtime_dir("tenants")

    def _new_state(self, tenant_key: str | None) -> TenantState:
        tenant = sanitize_tenant_key(tenant_key)
        now_ms = _now_ms()
        return TenantState(
            tenant_key=tenant,
            updated_at_ms=now_ms,
            heartbeat=TenantHeartbeatState(
                enabled=self._default_heartbeat_enabled,
                interval_s=self._default_heartbeat_interval_s,
                next_run_at_ms=now_ms + self._default_heartbeat_interval_s * 1000 + self._stable_stagger_ms(tenant),
            ),
        )

    def _stable_stagger_ms(self, tenant_key: str) -> int:
        """Return a stable per-tenant heartbeat stagger."""
        if self._default_heartbeat_stagger_s <= 0:
            return 0
        digest = hashlib.sha256(tenant_key.encode("utf-8")).digest()
        raw = int.from_bytes(digest[:8], "big")
        return raw % (self._default_heartbeat_stagger_s * 1000 + 1)

    def _deserialize(self, tenant_key: str | None, payload: dict) -> TenantState:
        state = self._new_state(tenant_key)
        hb_payload = payload.get("heartbeat", {})
        state.primary_session_key = payload.get("primarySessionKey")
        state.primary_channel = payload.get("primaryChannel")
        state.primary_chat_id = payload.get("primaryChatId")
        state.last_user_activity_at_ms = payload.get("lastUserActivityAtMs")
        state.updated_at_ms = payload.get("updatedAtMs", state.updated_at_ms)
        state.heartbeat = TenantHeartbeatState(
            enabled=hb_payload.get("enabled", state.heartbeat.enabled),
            interval_s=hb_payload.get("intervalS", state.heartbeat.interval_s),
            next_run_at_ms=hb_payload.get("nextRunAtMs", state.heartbeat.next_run_at_ms),
            last_run_at_ms=hb_payload.get("lastRunAtMs"),
            last_status=hb_payload.get("lastStatus"),
            last_error=hb_payload.get("lastError"),
        )
        return state

    def _serialize(self, state: TenantState) -> dict:
        return {
            "tenantKey": state.tenant_key,
            "primarySessionKey": state.primary_session_key,
            "primaryChannel": state.primary_channel,
            "primaryChatId": state.primary_chat_id,
            "lastUserActivityAtMs": state.last_user_activity_at_ms,
            "updatedAtMs": state.updated_at_ms,
            "heartbeat": {
                "enabled": state.heartbeat.enabled,
                "intervalS": state.heartbeat.interval_s,
                "nextRunAtMs": state.heartbeat.next_run_at_ms,
                "lastRunAtMs": state.heartbeat.last_run_at_ms,
                "lastStatus": state.heartbeat.last_status,
                "lastError": state.heartbeat.last_error,
            },
        }

    def get(self, tenant_key: str | None) -> TenantState | None:
        path = self._state_path(tenant_key)
        if not path.exists():
            return None
        with _locked_file(path) as (_, f):
            raw = f.read().strip()
            if not raw:
                return None
            return self._deserialize(tenant_key, json.loads(raw))

    def get_or_create(self, tenant_key: str | None) -> TenantState:
        state = self.get(tenant_key)
        if state is not None:
            return state
        state = self._new_state(tenant_key)
        self.save(state)
        return state

    def save(self, state: TenantState) -> TenantState:
        path = self._state_path(state.tenant_key)
        state.updated_at_ms = _now_ms()
        payload = json.dumps(self._serialize(state), ensure_ascii=False, indent=2)
        with _locked_file(path) as (_, f):
            f.seek(0)
            f.truncate()
            f.write(payload)
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
            state.heartbeat.next_run_at_ms = (
                _now_ms()
                + state.heartbeat.interval_s * 1000
                + self._stable_stagger_ms(state.tenant_key)
            )
        return self.save(state)

    def mark_heartbeat_result(
        self,
        tenant_key: str | None,
        *,
        status: Literal["ok", "skip", "defer", "error", "queued"],
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
        tenants_root = self._tenants_root()
        if not tenants_root.exists():
            return []
        states: list[TenantState] = []
        for tenant_dir in tenants_root.iterdir():
            if not tenant_dir.is_dir():
                continue
            path = tenant_dir / "state" / "tenant_state.json"
            if not path.exists():
                continue
            state = self.get(tenant_dir.name)
            if state is not None:
                states.append(state)
        return sorted(states, key=lambda item: item.updated_at_ms, reverse=True)

    def list_due_heartbeat_tenants(
        self,
        *,
        now_ms: int | None = None,
        tenant_filter: str | None = None,
    ) -> list[TenantState]:
        target_tenant = sanitize_tenant_key(tenant_filter) if tenant_filter else None
        due_at = now_ms or _now_ms()
        items = self.list_states()
        if target_tenant:
            items = [item for item in items if item.tenant_key == target_tenant]
        return [
            item
            for item in items
            if item.heartbeat.enabled
            and item.heartbeat.next_run_at_ms is not None
            and item.heartbeat.next_run_at_ms <= due_at
        ]
