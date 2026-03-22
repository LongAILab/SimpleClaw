"""Shared background-task protocol for multi-service runtime roles."""

from __future__ import annotations

import json
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


TaskStream = Literal["postprocess", "background", "outbound", "dead-letter"]


def now_ms() -> int:
    """Return the current unix timestamp in milliseconds."""
    return int(time.time() * 1000)


def make_trace_id() -> str:
    """Return a compact trace ID for one runtime request/task."""
    return uuid.uuid4().hex


def make_consumer_name(role: str) -> str:
    """Return a human-readable Redis consumer name."""
    return f"{role}:{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


@dataclass(slots=True)
class TaskEnvelope:
    """Serializable task envelope shared across services."""

    task_type: str
    payload: dict[str, Any]
    stream: TaskStream
    tenant_key: str | None = None
    session_key: str | None = None
    trace_id: str = field(default_factory=make_trace_id)
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    attempt: int = 0
    max_attempts: int = 3
    created_at_ms: int = field(default_factory=now_ms)
    service_role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize one task envelope to a plain dict."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize one task envelope to JSON."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskEnvelope":
        """Deserialize one task envelope from a dict."""
        return cls(
            task_type=str(payload["task_type"]),
            payload=dict(payload.get("payload") or {}),
            stream=str(payload["stream"]),
            tenant_key=payload.get("tenant_key"),
            session_key=payload.get("session_key"),
            trace_id=str(payload.get("trace_id") or make_trace_id()),
            task_id=str(payload.get("task_id") or uuid.uuid4().hex),
            attempt=int(payload.get("attempt") or 0),
            max_attempts=int(payload.get("max_attempts") or 3),
            created_at_ms=int(payload.get("created_at_ms") or now_ms()),
            service_role=payload.get("service_role"),
        )

    @classmethod
    def from_json(cls, payload: str) -> "TaskEnvelope":
        """Deserialize one task envelope from JSON."""
        return cls.from_dict(json.loads(payload))

