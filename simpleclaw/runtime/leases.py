"""Cross-process lease repository for background scheduling."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from simpleclaw.config.paths import get_runtime_subdir


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_key(raw: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw) or "lease"


@contextmanager
def _locked_file(path: Path) -> Iterator[object]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        yield f
        f.flush()
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@dataclass
class LeaseRecord:
    """One persisted lease."""

    scope: str
    key: str
    owner: str
    expires_at_ms: int


class LeaseRepository:
    """File-backed lease repository for multi-process coordination."""

    def __init__(self, owner_id: str | None = None, runtime_root: Path | None = None) -> None:
        self.owner_id = owner_id or str(uuid.uuid4())
        self.runtime_root = runtime_root

    def _lease_path(self, scope: str, key: str) -> Path:
        if self.runtime_root is not None:
            base = self.runtime_root / "leases"
            base.mkdir(parents=True, exist_ok=True)
        else:
            base = get_runtime_subdir("leases")
        return base / _safe_key(scope) / f"{_safe_key(key)}.json"

    def _load(self, path: Path) -> LeaseRecord | None:
        if not path.exists():
            return None
        with _locked_file(path) as f:
            raw = f.read().strip()
            if not raw:
                return None
            payload = json.loads(raw)
            return LeaseRecord(
                scope=str(payload.get("scope") or ""),
                key=str(payload.get("key") or ""),
                owner=str(payload.get("owner") or ""),
                expires_at_ms=int(payload.get("expiresAtMs") or 0),
            )

    def _write(self, path: Path, record: LeaseRecord) -> None:
        payload = json.dumps(
            {
                "scope": record.scope,
                "key": record.key,
                "owner": record.owner,
                "expiresAtMs": record.expires_at_ms,
            },
            ensure_ascii=False,
            indent=2,
        )
        with _locked_file(path) as f:
            f.seek(0)
            f.truncate()
            f.write(payload)

    def acquire(
        self,
        scope: str,
        key: str,
        *,
        ttl_s: int,
        owner: str | None = None,
    ) -> bool:
        """Acquire or renew a lease when it is free or already owned by caller."""
        lease_owner = owner or self.owner_id
        path = self._lease_path(scope, key)
        now_ms = _now_ms()
        expires_at_ms = now_ms + max(1, ttl_s) * 1000
        with _locked_file(path) as f:
            raw = f.read().strip()
            current: LeaseRecord | None = None
            if raw:
                payload = json.loads(raw)
                current = LeaseRecord(
                    scope=str(payload.get("scope") or scope),
                    key=str(payload.get("key") or key),
                    owner=str(payload.get("owner") or ""),
                    expires_at_ms=int(payload.get("expiresAtMs") or 0),
                )
            if current and current.expires_at_ms > now_ms and current.owner != lease_owner:
                return False
            f.seek(0)
            f.truncate()
            f.write(json.dumps(
                {
                    "scope": scope,
                    "key": key,
                    "owner": lease_owner,
                    "expiresAtMs": expires_at_ms,
                },
                ensure_ascii=False,
                indent=2,
            ))
        return True

    def renew(self, scope: str, key: str, *, ttl_s: int, owner: str | None = None) -> bool:
        """Renew an owned lease."""
        return self.acquire(scope, key, ttl_s=ttl_s, owner=owner)

    def release(self, scope: str, key: str, *, owner: str | None = None) -> None:
        """Release a lease owned by the caller."""
        lease_owner = owner or self.owner_id
        path = self._lease_path(scope, key)
        if not path.exists():
            return
        with _locked_file(path) as f:
            raw = f.read().strip()
            if not raw:
                return
            payload = json.loads(raw)
            current_owner = str(payload.get("owner") or "")
            if current_owner and current_owner != lease_owner:
                return
            f.seek(0)
            f.truncate()

    def is_active(self, scope: str, key: str) -> bool:
        """Return whether a non-expired lease exists."""
        record = self._load(self._lease_path(scope, key))
        return bool(record and record.expires_at_ms > _now_ms())
