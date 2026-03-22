"""Session management for conversation history."""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.config.paths import get_legacy_sessions_dir, sanitize_tenant_key
from nanobot.utils.helpers import ensure_dir, estimate_message_tokens, safe_filename


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    The consolidation process writes summaries to MEMORY.md/HISTORY.md
    but does NOT modify the messages list or get_history() output.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    @staticmethod
    def _to_history_entries(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Project persisted messages into provider-facing history entries."""
        out: list[dict[str, Any]] = []
        for m in messages:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]
            out.append(entry)
        return out

    @staticmethod
    def _drop_leading_non_user(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop leading tool/assistant blocks until the first user turn."""
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                return messages[i:]
        return []

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a user turn."""
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]
        return self._to_history_entries(self._drop_leading_non_user(sliced))

    def get_recent_history(self, max_tokens: int) -> list[dict[str, Any]]:
        """Return the newest complete user-aligned suffix that fits the token budget."""
        unconsolidated = self.messages[self.last_consolidated:]
        if not unconsolidated or max_tokens <= 0:
            return []

        suffix_tokens = [0] * (len(unconsolidated) + 1)
        for i in range(len(unconsolidated) - 1, -1, -1):
            suffix_tokens[i] = suffix_tokens[i + 1] + estimate_message_tokens(unconsolidated[i])

        user_starts = [i for i, message in enumerate(unconsolidated) if message.get("role") == "user"]
        if not user_starts:
            return self._to_history_entries(unconsolidated)

        best_start = user_starts[-1]
        for start in reversed(user_starts):
            if suffix_tokens[start] <= max_tokens:
                best_start = start
            else:
                break

        return self._to_history_entries(unconsolidated[best_start:])

    def get_unconsolidated_token_count(self) -> int:
        """Estimate token cost of the unconsolidated tail."""
        return sum(estimate_message_tokens(message) for message in self.messages[self.last_consolidated:])

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path, tenant_key: str | None = None, *, remote_only: bool = False):
        self.workspace = workspace
        self.tenant_key = sanitize_tenant_key(tenant_key)
        self.remote_only = remote_only
        self.sessions_dir = ensure_dir(self.workspace / "sessions") if not remote_only else self.workspace / "sessions"
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}

    def _cache_key(self, key: str, tenant_key: str | None = None) -> str:
        tenant = sanitize_tenant_key(tenant_key or self.tenant_key)
        return f"{tenant}:{key}"

    def _tenant_dir(self, tenant_key: str | None = None) -> Path:
        tenant = sanitize_tenant_key(tenant_key or self.tenant_key)
        if self.tenant_key != "__default__":
            return self.sessions_dir
        if tenant == "__default__":
            return self.sessions_dir
        path = self.sessions_dir / tenant
        return ensure_dir(path) if not self.remote_only else path

    def _get_session_path(self, key: str, tenant_key: str | None = None) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self._tenant_dir(tenant_key) / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.nanobot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str, tenant_key: str | None = None) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        cache_key = self._cache_key(key, tenant_key)
        if cache_key in self._cache:
            return self._cache[cache_key]

        session = self._load(key, tenant_key)
        if session is None:
            session = Session(key=key)

        self._cache[cache_key] = session
        return session

    def _load(self, key: str, tenant_key: str | None = None) -> Session | None:
        """Load a session from disk."""
        tenant = sanitize_tenant_key(tenant_key or self.tenant_key)
        path = self._get_session_path(key, tenant)
        if not path.exists() and tenant == "__default__":
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None

    def save(self, session: Session, tenant_key: str | None = None) -> None:
        """Save a session to disk."""
        tenant = sanitize_tenant_key(tenant_key or self.tenant_key)
        path = self._get_session_path(session.key, tenant)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[self._cache_key(session.key, tenant)] = session

    def invalidate(self, key: str, tenant_key: str | None = None) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(self._cache_key(key, tenant_key), None)

    def list_sessions(self, tenant_key: str | None = None) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        tenant = sanitize_tenant_key(tenant_key or self.tenant_key)
        search_dirs: list[tuple[str, Path]] = []
        if tenant == "__default__":
            search_dirs.append((tenant, self.sessions_dir))
            for item in self.sessions_dir.iterdir():
                if item.is_dir():
                    search_dirs.append((item.name, item))
            tenants_root = self.workspace / "tenants"
            if tenants_root.exists():
                for item in tenants_root.iterdir():
                    sessions_dir = item / "sessions"
                    if item.is_dir() and sessions_dir.exists():
                        search_dirs.append((item.name, sessions_dir))
        else:
            search_dirs.append((tenant, self._tenant_dir(tenant)))

        for current_tenant, base_dir in search_dirs:
            for path in base_dir.glob("*.jsonl"):
                try:
                    # Read just the metadata line
                    with open(path, encoding="utf-8") as f:
                        first_line = f.readline().strip()
                        if first_line:
                            data = json.loads(first_line)
                            if data.get("_type") == "metadata":
                                key = data.get("key") or path.stem.replace("_", ":", 1)
                                sessions.append({
                                    "tenant_key": current_tenant,
                                    "key": key,
                                    "created_at": data.get("created_at"),
                                    "updated_at": data.get("updated_at"),
                                    "path": str(path)
                                })
                except Exception:
                    continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
