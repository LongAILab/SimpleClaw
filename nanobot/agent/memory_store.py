"""Persistent long-term memory and history archive storage."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_additions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "New facts to add to long-term memory. Each item is one line "
                        "(for example '- 肤质：敏感肌'). Only include genuinely new facts not already in memory.",
                    },
                    "memory_removals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Facts to remove from long-term memory because they are outdated or "
                        "contradicted. Each item must be the exact line to remove. Usually empty.",
                    },
                },
                "required": ["history_entry", "memory_additions", "memory_removals"],
            },
        },
    }
]

_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_text_list(value: Any) -> list[str]:
    """Normalize one tool payload field into a compact list of non-empty lines."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = item if isinstance(item, str) else str(item or "")
        stripped = text.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        normalized.append(stripped)
    return normalized


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None


def _is_tool_choice_unsupported(content: str | None) -> bool:
    """Detect provider errors caused by forced tool_choice being unsupported."""
    text = (content or "").lower()
    return any(marker in text for marker in _TOOL_CHOICE_ERROR_MARKERS)


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (searchable log)."""

    _MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3

    def __init__(
        self,
        workspace: Path,
        *,
        tenant_key: str | None = None,
        repository: Any | None = None,
    ):
        self.memory_dir = ensure_dir(workspace / "memory") if repository is None else workspace / "memory"
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self.tenant_key = tenant_key or "__default__"
        self.repository = repository
        self._consecutive_failures = 0
        self._last_archive_entry: str | None = None

    def read_long_term(self) -> str:
        if self.repository is not None:
            return self.repository.read_long_term(self.tenant_key)
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        if self.repository is not None:
            self.repository.save_snapshot(self.tenant_key, long_term_markdown=content)
            return
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        if self.repository is not None:
            source_type = "manual"
            event_type = "conversation_recap"
            if "[STRUCTURED]" in entry:
                source_type = "structured_memory"
                event_type = "profile_fact"
            elif "[RAW]" in entry:
                source_type = "raw_archive"
                event_type = "raw_archive"
            else:
                source_type = "consolidation"
            self.repository.append_event(
                self.tenant_key,
                history_entry=entry.rstrip(),
                source_type=source_type,
                event_type=event_type,
            )
            return
        with open(self.history_file, "a", encoding="utf-8") as handle:
            handle.write(entry.rstrip() + "\n\n")

    def overwrite_history(self, content: str) -> None:
        """Replace the history archive contents."""
        if self.repository is not None:
            self.repository.overwrite_history(self.tenant_key, content)
            return
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        self.history_file.write_text(content, encoding="utf-8")

    def get_memory_context(self) -> str:
        return self.read_long_term().strip()

    def consume_last_archive_entry(self) -> str | None:
        """Return the newest archive entry generated by consolidation."""
        entry = self._last_archive_entry
        self._last_archive_entry = None
        return entry

    @staticmethod
    def _content_to_text(content: Any) -> str:
        """Convert one message content payload into plain text for consolidation."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
                elif item.get("type") == "image_url":
                    parts.append("[用户上传了一张图片]")
            return " ".join(parts)
        return str(content or "")

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            text = MemoryStore._content_to_text(message.get("content")).strip()
            if not text:
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {text}"
            )
        return "\n".join(lines)

    @staticmethod
    def _merge_memory_patch(current_memory: str, additions: list[str], removals: list[str]) -> str:
        """Apply a simple additive/removal patch to MEMORY.md without full overwrite."""
        updated = current_memory.rstrip()
        removal_set = {line.strip() for line in removals if line.strip()}
        if removal_set:
            kept_lines = [
                line
                for line in updated.splitlines()
                if line.strip() not in removal_set
            ]
            updated = "\n".join(kept_lines).rstrip()

        existing_lines = {line.strip() for line in updated.splitlines() if line.strip()}
        new_lines: list[str] = []
        for line in additions:
            stripped = line.strip()
            if not stripped or stripped in existing_lines:
                continue
            existing_lines.add(stripped)
            new_lines.append(stripped)

        if not new_lines:
            return updated
        if not updated:
            return "\n".join(new_lines)
        separator = "\n\n" if not updated.endswith("\n") else "\n"
        return f"{updated}{separator}" + "\n".join(new_lines)

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        """Consolidate the provided message chunk into MEMORY.md + HISTORY.md."""
        if not messages:
            return True
        self._last_archive_entry = None

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation patch.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{self._format_messages(messages)}

Instructions:
- Return only a concise history_entry plus a memory patch.
- memory_additions should contain only genuinely new long-term facts, one fact per line.
- memory_removals should contain exact existing lines that should be removed because they are outdated or contradicted.
- Do not rewrite the full MEMORY.md contents.
- If nothing should change in long-term memory, return empty arrays for memory_additions and memory_removals."""

        chat_messages = [
            {
                "role": "system",
                "content": "You are a memory consolidation agent. Call the save_memory tool with a history summary and a minimal memory patch only.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            forced = {"type": "function", "function": {"name": "save_memory"}}
            response = await provider.chat_with_retry(
                messages=chat_messages,
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                tool_choice=forced,
            )

            if response.finish_reason == "error" and _is_tool_choice_unsupported(response.content):
                logger.warning("Forced tool_choice unsupported, retrying with auto")
                response = await provider.chat_with_retry(
                    messages=chat_messages,
                    tools=_SAVE_MEMORY_TOOL,
                    model=model,
                    tool_choice="auto",
                )

            if not response.has_tool_calls:
                logger.warning(
                    "Memory consolidation: LLM did not call save_memory "
                    "(finish_reason={}, content_len={}, content_preview={})",
                    response.finish_reason,
                    len(response.content or ""),
                    (response.content or "")[:200],
                )
                return self._fail_or_raw_archive(messages)

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return self._fail_or_raw_archive(messages)

            if (
                "history_entry" not in args
                or "memory_additions" not in args
                or "memory_removals" not in args
            ):
                logger.warning("Memory consolidation: save_memory payload missing required fields")
                return self._fail_or_raw_archive(messages)

            entry = args["history_entry"]
            additions = args["memory_additions"]
            removals = args["memory_removals"]

            if entry is None or additions is None or removals is None:
                logger.warning("Memory consolidation: save_memory payload contains null required fields")
                return self._fail_or_raw_archive(messages)

            entry = _ensure_text(entry).strip()
            if not entry:
                logger.warning("Memory consolidation: history_entry is empty after normalization")
                return self._fail_or_raw_archive(messages)

            self.append_history(entry)
            self._last_archive_entry = entry
            additions_list = _normalize_text_list(additions)
            removals_list = _normalize_text_list(removals)
            update = self._merge_memory_patch(current_memory, additions_list, removals_list)
            if update != current_memory:
                self.write_long_term(update)

            self._consecutive_failures = 0
            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return self._fail_or_raw_archive(messages)

    def _fail_or_raw_archive(self, messages: list[dict]) -> bool:
        """Increment failure count; after threshold, raw-archive messages and return True."""
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_RAW_ARCHIVE:
            return False
        self._raw_archive(messages)
        self._consecutive_failures = 0
        return True

    def _raw_archive(self, messages: list[dict]) -> None:
        """Fallback: dump raw messages to HISTORY.md without LLM summarization."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = (
            f"[{ts}] [RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        self.append_history(entry)
        self._last_archive_entry = entry
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )
