import pytest

from simpleclaw.heartbeat.scheduler import HeartbeatScheduler, HeartbeatTarget
from simpleclaw.config.paths import get_tenant_workspace_path
from simpleclaw.runtime.leases import LeaseRepository
from simpleclaw.tenant.state import TenantStateRepository


@pytest.mark.asyncio
async def test_scheduler_runs_due_tenant(tmp_path) -> None:
    tenant_workspace = get_tenant_workspace_path(tmp_path, "tenant-a")
    (tenant_workspace / "HEARTBEAT.md").write_text("content", encoding="utf-8")
    tenant_repo = TenantStateRepository(
        runtime_root=tmp_path / "runtime",
        default_heartbeat_enabled=True,
        default_heartbeat_interval_s=60,
    )
    lease_repo = LeaseRepository(owner_id="scheduler", runtime_root=tmp_path / "runtime")
    tenant_repo.touch_interaction(
        tenant_key="tenant-a",
        session_key="api:conv-1",
        channel="api",
        chat_id="conv-1",
        activity_at_ms=0,
    )
    tenant_repo.configure_heartbeat("tenant-a", next_run_at_ms=0)
    seen: list[HeartbeatTarget] = []

    async def _runner(target: HeartbeatTarget, content: str) -> str:
        assert content == "content"
        seen.append(target)
        return "ok"

    scheduler = HeartbeatScheduler(
        workspace=tmp_path,
        tenant_repo=tenant_repo,
        lease_repo=lease_repo,
        runner=_runner,
        poll_interval_s=1,
        busy_defer_s=30,
        recent_activity_cooldown_s=0,
        max_concurrency=1,
    )

    await scheduler._scan_once()

    assert [item.tenant_key for item in seen] == ["tenant-a"]


@pytest.mark.asyncio
async def test_scheduler_defers_recently_active_tenant(tmp_path) -> None:
    tenant_workspace = get_tenant_workspace_path(tmp_path, "tenant-a")
    (tenant_workspace / "HEARTBEAT.md").write_text("content", encoding="utf-8")
    tenant_repo = TenantStateRepository(
        runtime_root=tmp_path / "runtime",
        default_heartbeat_enabled=True,
        default_heartbeat_interval_s=60,
    )
    lease_repo = LeaseRepository(owner_id="scheduler", runtime_root=tmp_path / "runtime")
    tenant_repo.touch_interaction(
        tenant_key="tenant-a",
        session_key="api:conv-1",
        channel="api",
        chat_id="conv-1",
    )
    tenant_repo.configure_heartbeat("tenant-a", next_run_at_ms=0)

    async def _runner(_target: HeartbeatTarget, _content: str) -> str:
        raise AssertionError("runner should not execute for recently active tenant")

    scheduler = HeartbeatScheduler(
        workspace=tmp_path,
        tenant_repo=tenant_repo,
        lease_repo=lease_repo,
        runner=_runner,
        poll_interval_s=1,
        busy_defer_s=30,
        recent_activity_cooldown_s=3600,
        max_concurrency=1,
    )

    await scheduler._scan_once()

    state = tenant_repo.get("tenant-a")
    assert state is not None
    assert state.heartbeat.last_status == "defer"


@pytest.mark.asyncio
async def test_scheduler_ignores_shared_base_heartbeat_file(tmp_path) -> None:
    (tmp_path / "base").mkdir(parents=True, exist_ok=True)
    (tmp_path / "base" / "HEARTBEAT.md").write_text("content", encoding="utf-8")

    tenant_repo = TenantStateRepository(
        runtime_root=tmp_path / "runtime",
        default_heartbeat_enabled=True,
        default_heartbeat_interval_s=60,
    )
    lease_repo = LeaseRepository(owner_id="scheduler", runtime_root=tmp_path / "runtime")
    tenant_repo.touch_interaction(
        tenant_key="tenant-a",
        session_key="api:conv-1",
        channel="api",
        chat_id="conv-1",
        activity_at_ms=0,
    )
    tenant_repo.configure_heartbeat("tenant-a", next_run_at_ms=0)

    seen: list[HeartbeatTarget] = []

    async def _runner(target: HeartbeatTarget, content: str) -> str:
        seen.append(target)
        return "ok"

    scheduler = HeartbeatScheduler(
        workspace=tmp_path,
        tenant_repo=tenant_repo,
        lease_repo=lease_repo,
        runner=_runner,
        poll_interval_s=1,
        busy_defer_s=30,
        recent_activity_cooldown_s=0,
        max_concurrency=1,
    )

    await scheduler._scan_once()

    assert seen == []
