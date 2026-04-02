"""Stateless helpers for one conversational turn."""

from __future__ import annotations

import re
from typing import Any, Callable

from simpleclaw.session.manager import Session


def get_extra_system_sections(metadata: dict[str, Any] | None) -> list[str] | None:
    """Extract transient system-prompt sections from message metadata."""
    if not metadata:
        return None
    sections = metadata.get("_extra_system_sections")
    if not isinstance(sections, list):
        return None
    return [section for section in sections if isinstance(section, str) and section.strip()]


def get_outbound_message_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Attach SSE event metadata for background lanes using message tool."""
    if not metadata:
        return None
    lane = metadata.get("_lane")
    if lane == "cron":
        return {
            "_event": "cron_message",
            "_source": "cron",
        }
    if lane == "heartbeat":
        return {
            "_event": "heartbeat_message",
            "_source": "heartbeat",
        }
    return None


def get_outbound_session_key(metadata: dict[str, Any] | None, fallback: str) -> str:
    """Use UI-facing session key for outbound messages when provided."""
    if not metadata:
        return fallback
    outbound_session_key = metadata.get("_outbound_session_key")
    if isinstance(outbound_session_key, str) and outbound_session_key.strip():
        return outbound_session_key.strip()
    return fallback


def get_history_for_turn(
    session: Session,
    metadata: dict[str, Any] | None,
    history_selector: Callable[[Session], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Return session history unless this turn explicitly suppresses it."""
    if metadata and metadata.get("_suppress_history"):
        return []
    if history_selector is not None:
        return history_selector(session)
    return session.get_history(max_messages=0)


def strip_think(text: str | None) -> str | None:
    """Remove <think>…</think> blocks that some models embed in content."""
    if not text:
        return None
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None


def tool_hint(tool_calls: list) -> str:
    """Format tool calls as concise hint, e.g. 'web_search(\"query\")'."""

    def _fmt(tc):
        args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
        val = next(iter(args.values()), None) if isinstance(args, dict) else None
        if not isinstance(val, str):
            return tc.name
        return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'

    return ", ".join(_fmt(tc) for tc in tool_calls)


def matches_deferred_path(path: Any, expected_suffix: str) -> bool:
    """Check whether a tool path targets one deferred-write file."""
    if not isinstance(path, str):
        return False
    normalized = path.replace("\\", "/").lower()
    suffix = expected_suffix.replace("\\", "/").lower()
    return normalized.endswith(suffix) or normalized == suffix.lstrip("/")


def derive_session_type(metadata: dict[str, Any] | None, session_key: str) -> str:
    """Resolve the persisted session type for this turn."""
    if metadata:
        explicit = metadata.get("_session_type")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
    if session_key.startswith("postprocess:"):
        return "postprocess"
    if session_key.startswith("cron:"):
        return "cron"
    return "main"


def apply_session_metadata(
    session: Session,
    *,
    session_key: str,
    channel: str,
    chat_id: str,
    metadata: dict[str, Any] | None,
) -> None:
    """Persist stable routing/session semantics onto the session object."""
    merged = dict(session.metadata or {})
    session_type = derive_session_type(metadata, session_key)
    merged["session_type"] = session_type
    merged["channel"] = channel
    merged["chat_id"] = chat_id
    if metadata:
        origin_session_key = metadata.get("_origin_session_key")
        if isinstance(origin_session_key, str) and origin_session_key.strip():
            merged["origin_session_key"] = origin_session_key.strip()
        elif session_type == "main":
            merged.pop("origin_session_key", None)
        if metadata.get("_track_primary_session") is True and session_type == "main":
            merged["is_primary"] = True
    elif session_type == "main":
        merged.pop("origin_session_key", None)
    session.metadata = merged
