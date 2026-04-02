"""Migration helpers from file-backed runtime state to MySQL."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from simpleclaw.config.paths import get_cron_dir
from simpleclaw.config.schema import Config
from simpleclaw.cron.repository import CronRepository
from simpleclaw.session.manager import SessionManager
from simpleclaw.session.mysql import MySQLSessionManager
from simpleclaw.storage.mysql import (
    MySQLCronRepository,
    MySQLDatabase,
    MySQLSessionStore,
    MySQLTenantStateRepository,
)
from simpleclaw.tenant.state import TenantStateRepository


def migrate_file_runtime_state_to_mysql(config: Config, console: Console) -> None:
    """Copy file-backed runtime state into the configured MySQL tables."""
    if not config.runtime.mysql.enabled:
        raise RuntimeError("MySQL runtime config must be enabled before migration.")

    db = MySQLDatabase(
        host=config.runtime.mysql.host,
        port=config.runtime.mysql.port,
        user=config.runtime.mysql.user,
        password=config.runtime.mysql.password,
        database=config.runtime.mysql.database,
        charset=config.runtime.mysql.charset,
        connect_timeout=config.runtime.mysql.connect_timeout,
    )
    db.ensure_schema()
    tenant_target = MySQLTenantStateRepository(
        db,
        default_heartbeat_enabled=config.gateway.heartbeat.enabled,
        default_heartbeat_interval_s=config.gateway.heartbeat.interval_s,
        default_heartbeat_stagger_s=config.gateway.heartbeat.stagger_s,
    )
    cron_target = MySQLCronRepository(db)
    session_store = MySQLSessionStore(db)

    tenant_source = TenantStateRepository(
        default_heartbeat_enabled=config.gateway.heartbeat.enabled,
        default_heartbeat_interval_s=config.gateway.heartbeat.interval_s,
        default_heartbeat_stagger_s=config.gateway.heartbeat.stagger_s,
    )
    migrated_tenants = 0
    for state in tenant_source.list_states():
        tenant_target.save(state)
        migrated_tenants += 1

    cron_source = CronRepository(get_cron_dir() / "jobs.json")
    migrated_cron = 0
    for job in cron_source.list_jobs(include_disabled=True):
        cron_target.save_job(job)
        migrated_cron += 1

    source_sessions = SessionManager(config.workspace_path)
    target_sessions = MySQLSessionManager(
        config.workspace_path,
        session_store=session_store,
        tenant_key="__default__",
    )
    migrated_sessions = 0
    for item in source_sessions.list_sessions():
        tenant_key = item.get("tenant_key") or "__default__"
        session_key = str(item["key"])
        current = SessionManager(config.workspace_path, tenant_key=tenant_key).get_or_create(
            session_key,
            tenant_key=tenant_key,
        )
        target_sessions.save(current, tenant_key=tenant_key)
        migrated_sessions += 1

    console.print(
        "[green]✓[/green] Migrated runtime state to MySQL "
        f"(tenants={migrated_tenants}, cron_jobs={migrated_cron}, sessions={migrated_sessions})"
    )

