from nanobot.tenant.state import TenantStateRepository


def test_touch_interaction_sets_primary_session_and_activity(tmp_path) -> None:
    repo = TenantStateRepository(
        runtime_root=tmp_path / "runtime",
        default_heartbeat_enabled=True,
        default_heartbeat_interval_s=60,
    )

    state = repo.touch_interaction(
        tenant_key="tenant-a",
        session_key="api:conv-1",
        channel="api",
        chat_id="conv-1",
        activity_at_ms=1234,
    )

    assert state.tenant_key == "tenant-a"
    assert state.primary_session_key == "api:conv-1"
    assert state.primary_channel == "api"
    assert state.primary_chat_id == "conv-1"
    assert state.last_user_activity_at_ms == 1234
    assert state.heartbeat.next_run_at_ms is not None


def test_due_heartbeat_tenants_uses_persisted_schedule(tmp_path) -> None:
    repo = TenantStateRepository(
        runtime_root=tmp_path / "runtime",
        default_heartbeat_enabled=True,
        default_heartbeat_interval_s=60,
    )
    repo.touch_interaction(
        tenant_key="tenant-a",
        session_key="api:conv-1",
        channel="api",
        chat_id="conv-1",
        activity_at_ms=1000,
    )
    repo.configure_heartbeat("tenant-a", next_run_at_ms=2000)

    due = repo.list_due_heartbeat_tenants(now_ms=2500)

    assert [item.tenant_key for item in due] == ["tenant-a"]


def test_mark_heartbeat_result_updates_state(tmp_path) -> None:
    repo = TenantStateRepository(
        runtime_root=tmp_path / "runtime",
        default_heartbeat_enabled=True,
        default_heartbeat_interval_s=60,
    )
    repo.touch_interaction(
        tenant_key="tenant-a",
        session_key="api:conv-1",
        channel="api",
        chat_id="conv-1",
        activity_at_ms=1000,
    )

    state = repo.mark_heartbeat_result(
        "tenant-a",
        status="defer",
        next_run_at_ms=9999,
        error="busy",
        ran_at_ms=2000,
    )

    assert state.heartbeat.last_status == "defer"
    assert state.heartbeat.last_error == "busy"
    assert state.heartbeat.last_run_at_ms == 2000
    assert state.heartbeat.next_run_at_ms == 9999
