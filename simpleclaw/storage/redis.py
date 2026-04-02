"""Redis-backed coordination primitives."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


class RedisLeaseRepository:
    """Lease repository backed by Redis keys."""

    def __init__(self, *, url: str, key_prefix: str = "simpleclaw", owner_id: str | None = None) -> None:
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - dependency/runtime guard
            raise RuntimeError("Redis support requires the 'redis' package") from exc
        self._client = redis.Redis.from_url(url, decode_responses=True)
        self._key_prefix = key_prefix.rstrip(":")
        self.owner_id = owner_id or str(uuid.uuid4())

    def _key(self, scope: str, key: str) -> str:
        return f"{self._key_prefix}:lease:{scope}:{key}"

    def _payload(self, *, scope: str, key: str, owner: str, ttl_s: int) -> tuple[str, int]:
        expires_at_ms = _now_ms() + max(1, ttl_s) * 1000
        payload = json.dumps(
            {
                "scope": scope,
                "key": key,
                "owner": owner,
                "expiresAtMs": expires_at_ms,
            },
            ensure_ascii=False,
        )
        return payload, expires_at_ms

    def acquire(
        self,
        scope: str,
        key: str,
        *,
        ttl_s: int,
        owner: str | None = None,
    ) -> bool:
        lease_owner = owner or self.owner_id
        payload, _ = self._payload(scope=scope, key=key, owner=lease_owner, ttl_s=ttl_s)
        redis_key = self._key(scope, key)
        if self._client.set(redis_key, payload, ex=max(1, ttl_s), nx=True):
            return True
        existing = self._client.get(redis_key)
        if not existing:
            return bool(self._client.set(redis_key, payload, ex=max(1, ttl_s), nx=True))
        try:
            raw = json.loads(existing)
        except Exception:
            raw = {}
        if raw.get("owner") != lease_owner:
            return False
        self._client.set(redis_key, payload, ex=max(1, ttl_s))
        return True

    def renew(self, scope: str, key: str, *, ttl_s: int, owner: str | None = None) -> bool:
        return self.acquire(scope, key, ttl_s=ttl_s, owner=owner)

    def release(self, scope: str, key: str, *, owner: str | None = None) -> None:
        lease_owner = owner or self.owner_id
        redis_key = self._key(scope, key)
        existing = self._client.get(redis_key)
        if not existing:
            return
        try:
            raw = json.loads(existing)
        except Exception:
            raw = {}
        if raw.get("owner") and raw.get("owner") != lease_owner:
            return
        self._client.delete(redis_key)

    def is_active(self, scope: str, key: str) -> bool:
        return bool(self._client.exists(self._key(scope, key)))

