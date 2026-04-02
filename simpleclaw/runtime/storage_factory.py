"""Storage/queue backend construction helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from simpleclaw.config.schema import Config
from simpleclaw.runtime.leases import LeaseRepository
from simpleclaw.runtime.task_queue import InMemoryTaskQueue, RedisTaskQueue
from simpleclaw.storage.interfaces import CronJobStore, LeaseStore, SessionStore, TaskStateStore, TenantStateStore
from simpleclaw.storage.mysql import (
    MySQLCronRepository,
    MySQLDatabase,
    MySQLSessionStore,
    MySQLTenantDocumentStore,
    MySQLTenantMemoryStore,
    MySQLTenantProvisioner,
    MySQLTenantStateRepository,
)
from simpleclaw.storage.redis import RedisLeaseRepository
from simpleclaw.tenant.state import TenantStateRepository


@dataclass(slots=True)
class PersistenceBackends:
    mysql_db: MySQLDatabase | None
    tenant_state_repo: TenantStateStore
    cron_repository: CronJobStore | None
    session_store: SessionStore | None
    document_store: Any | None
    memory_store: Any | None
    tenant_provisioner: Any | None
    task_repository: TaskStateStore | None


@dataclass(slots=True)
class QueueBackends:
    task_queue: InMemoryTaskQueue | RedisTaskQueue
    lease_repo: LeaseStore


def build_persistence_backends(config: Config) -> PersistenceBackends:
    """Create tenant/session/document repositories for the active storage mode."""
    mysql_db = None
    document_store = None
    memory_store = None
    tenant_provisioner = None
    task_repository = None
    if config.runtime.mysql.enabled:
        mysql_db = MySQLDatabase(
            host=config.runtime.mysql.host,
            port=config.runtime.mysql.port,
            user=config.runtime.mysql.user,
            password=config.runtime.mysql.password,
            database=config.runtime.mysql.database,
            charset=config.runtime.mysql.charset,
            connect_timeout=config.runtime.mysql.connect_timeout,
        )
        mysql_db.ensure_schema()
        tenant_state_repo = MySQLTenantStateRepository(
            mysql_db,
            default_heartbeat_enabled=config.gateway.heartbeat.enabled,
            default_heartbeat_interval_s=config.gateway.heartbeat.interval_s,
            default_heartbeat_stagger_s=config.gateway.heartbeat.stagger_s,
        )
        cron_repository = MySQLCronRepository(mysql_db)
        session_store = MySQLSessionStore(mysql_db)
        document_store = MySQLTenantDocumentStore(mysql_db)
        memory_store = MySQLTenantMemoryStore(mysql_db)
        tenant_provisioner = MySQLTenantProvisioner(
            mysql_db,
            document_store=document_store,
            memory_store=memory_store,
        )
    else:
        tenant_state_repo = TenantStateRepository(
            default_heartbeat_enabled=config.gateway.heartbeat.enabled,
            default_heartbeat_interval_s=config.gateway.heartbeat.interval_s,
            default_heartbeat_stagger_s=config.gateway.heartbeat.stagger_s,
        )
        cron_repository = None
        session_store = None

    return PersistenceBackends(
        mysql_db=mysql_db,
        tenant_state_repo=tenant_state_repo,
        cron_repository=cron_repository,
        session_store=session_store,
        document_store=document_store,
        memory_store=memory_store,
        tenant_provisioner=tenant_provisioner,
        task_repository=task_repository,
    )


def build_queue_backends(config: Config) -> QueueBackends:
    """Create task queue + lease repository for the active runtime mode."""
    if config.runtime.redis.enabled:
        task_queue = RedisTaskQueue(
            url=config.runtime.redis.url,
            stream_prefix=config.runtime.redis.stream_prefix,
            stream_names={
                "postprocess": config.runtime.tasks.postprocess_stream,
                "background": config.runtime.tasks.background_stream,
                "outbound": config.runtime.tasks.outbound_stream,
                "dead-letter": config.runtime.tasks.dead_letter_stream,
            },
            block_ms=config.runtime.redis.consumer_block_ms,
            batch_size=config.runtime.redis.consumer_batch_size,
        )
        lease_repo = RedisLeaseRepository(
            url=config.runtime.redis.url,
            key_prefix=config.runtime.redis.key_prefix,
        )
    else:
        task_queue = InMemoryTaskQueue()
        lease_repo = LeaseRepository()
    return QueueBackends(task_queue=task_queue, lease_repo=lease_repo)
