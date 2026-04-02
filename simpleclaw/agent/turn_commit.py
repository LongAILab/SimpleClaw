"""Helpers for persisting one completed turn."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from simpleclaw.agent.context import ContextBuilder
from simpleclaw.session.manager import Session


def save_turn_messages(
    session: Session,
    messages: list[dict[str, Any]],
    *,
    skip: int,
    tool_result_max_chars: int,
) -> None:
    """Save new-turn messages into a session, truncating large tool results."""
    for message in messages[skip:]:
        entry = dict(message)
        role, content = entry.get("role"), entry.get("content")
        if role == "assistant" and not content and not entry.get("tool_calls"):
            continue
        if role == "tool" and isinstance(content, str) and len(content) > tool_result_max_chars:
            entry["content"] = content[:tool_result_max_chars] + "\n... (truncated)"
        elif role == "user":
            if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                parts = content.split("\n\n", 1)
                if len(parts) > 1 and parts[1].strip():
                    entry["content"] = parts[1]
                else:
                    continue
            if isinstance(content, list):
                filtered = []
                for chunk in content:
                    if (
                        chunk.get("type") == "text"
                        and isinstance(chunk.get("text"), str)
                        and chunk["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
                    ):
                        continue
                    if (
                        chunk.get("type") == "image_url"
                        and chunk.get("image_url", {}).get("url", "").startswith("data:image/")
                    ):
                        filtered.append({"type": "text", "text": "[image]"})
                    else:
                        filtered.append(chunk)
                if not filtered:
                    continue
                entry["content"] = filtered
        entry.setdefault("timestamp", datetime.now().isoformat())
        session.messages.append(entry)
    session.updated_at = datetime.now()
