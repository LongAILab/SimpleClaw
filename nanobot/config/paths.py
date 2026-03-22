"""Runtime path helpers derived from the active config context."""

from __future__ import annotations

from pathlib import Path

from nanobot.config.loader import get_config_path
from nanobot.utils.helpers import ensure_dir


def sanitize_tenant_key(tenant_key: str | None) -> str:
    """Return a filesystem-safe tenant namespace key."""
    if not tenant_key:
        return "__default__"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in tenant_key.strip())
    return safe or "__default__"


def get_data_dir() -> Path:
    """Return the instance-level runtime data directory."""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """Return a named runtime subdirectory under the instance data dir."""
    return ensure_dir(get_data_dir() / name)


def get_tenant_runtime_subdir(name: str, tenant_key: str | None = None) -> Path:
    """Return a runtime subdirectory scoped to one tenant."""
    tenant = sanitize_tenant_key(tenant_key)
    return ensure_dir(get_runtime_subdir("tenants") / tenant / name)


def get_media_dir(channel: str | None = None, tenant_key: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = get_tenant_runtime_subdir("media", tenant_key) if tenant_key else get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir(tenant_key: str | None = None) -> Path:
    """Return the cron storage directory."""
    if tenant_key:
        return get_tenant_runtime_subdir("cron", tenant_key)
    return get_runtime_subdir("cron")


def get_logs_dir(tenant_key: str | None = None) -> Path:
    """Return the logs directory."""
    if tenant_key:
        return get_tenant_runtime_subdir("logs", tenant_key)
    return get_runtime_subdir("logs")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    path = Path(workspace).expanduser() if workspace else Path.home() / ".nanobot" / "workspace"
    return ensure_dir(path)


def get_base_workspace_path(workspace: str | Path) -> Path:
    """Resolve the shared base workspace rooted under the instance workspace."""
    return ensure_dir(Path(workspace).expanduser() / "base")


def resolve_workspace_root(workspace: str | Path) -> Path:
    """Resolve the instance workspace root from a base/default/tenant workspace path."""
    path = Path(workspace).expanduser()
    if path.name == "base":
        return path.parent
    if path.parent.name == "tenants":
        return path.parent.parent
    return path


def get_tenant_workspace_path(workspace: str | Path, tenant_key: str | None, *, create: bool = True) -> Path:
    """Resolve a tenant-scoped workspace rooted under the instance workspace."""
    base = Path(workspace).expanduser()
    tenant = sanitize_tenant_key(tenant_key)
    if tenant == "__default__":
        return ensure_dir(base) if create else base
    path = base / "tenants" / tenant
    return ensure_dir(path) if create else path


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return Path.home() / ".nanobot" / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """Return the shared WhatsApp bridge installation directory."""
    return Path.home() / ".nanobot" / "bridge"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".nanobot" / "sessions"
