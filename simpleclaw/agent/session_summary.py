"""Helpers for denoised rolling session summaries."""

from __future__ import annotations

import re
from typing import Any

from simpleclaw.agent.context_budget import estimate_text_tokens

_SUMMARY_NOISE_MARKERS = (
    "heartbeat triggered",
    "heartbeat触发",
    "心跳触发",
    "check-in",
    "周期性会话",
    "主动问候",
    "正常运行中",
    "继续运行中",
    "继续运行",
    "无新增事项",
    "无新增事实",
    "无新增",
    "任务状态正常",
    "状态正常维持",
)

_SUMMARY_STRONG_CHANGE_MARKERS = (
    "创建",
    "已创建",
    "设置",
    "更新",
    "取消",
    "停止",
    "移除",
    "改为",
    "开始",
    "启动",
    "要求",
    "created",
    "updated",
    "cancel",
    "removed",
    "scheduled",
)


def is_noise_session_summary_entry(entry: str) -> bool:
    """Return True when one summary line is low-value status chatter."""
    text = " ".join(part.strip() for part in entry.splitlines() if part.strip())
    if not text:
        return True
    lowered = text.lower()
    has_noise = any(marker in lowered for marker in _SUMMARY_NOISE_MARKERS)
    has_strong_change = any(marker in lowered for marker in _SUMMARY_STRONG_CHANGE_MARKERS)
    has_heartbeat_context = any(
        marker in lowered
        for marker in ("heartbeat", "heartbeat触发", "心跳触发", "check-in", "周期性会话")
    )
    if has_heartbeat_context and not has_strong_change:
        return True
    return has_noise and not has_strong_change


def split_session_summary_entries(text: str) -> list[str]:
    """Split raw summary text into clean per-event entries."""
    entries: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        parts = re.split(r"(?=\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?:-\d{2}:\d{2})?\])", line)
        for part in parts:
            normalized = " ".join(segment.strip() for segment in part.splitlines() if segment.strip()).strip()
            if normalized:
                entries.append(normalized)
    return entries


def normalize_summary_entry(entry: str) -> str:
    """Convert one history paragraph into a compact rolling-summary bullet."""
    entries = split_session_summary_entries(entry)
    if not entries:
        return ""
    text = entries[-1]
    return text if text.startswith("- ") else f"- {text}"


def is_background_summary_chunk(messages: list[dict[str, Any]]) -> bool:
    """Detect heartbeat/postprocess chunks that should not pollute session summary."""
    user_messages = [
        str(message.get("content") or "").strip()
        for message in messages
        if str(message.get("role") or "") == "user"
    ]
    if not user_messages:
        return False
    background_prefixes = (
        "[Heartbeat Trigger]",
        "[Heartbeat Decision]",
        "[Deferred Postprocess Task]",
    )
    return all(any(text.startswith(prefix) for prefix in background_prefixes) for text in user_messages)


def merge_rolling_summary(
    existing: str,
    new_entry: str,
    *,
    max_entries: int,
    budget_tokens: int,
) -> str:
    """Append one summary bullet and trim from the front to fit the budget."""
    normalized = normalize_summary_entry(new_entry)
    if not normalized:
        return existing.strip()
    if is_noise_session_summary_entry(normalized):
        normalized = ""
    entries = []
    for line in split_session_summary_entries(existing):
        if not is_noise_session_summary_entry(line):
            entries.append(f"- {line}")
    if normalized:
        entries.append(normalized)
    if not entries:
        return ""
    kept: list[str] = []
    used = 0
    for entry in reversed(entries[-max_entries:]):
        entry_tokens = estimate_text_tokens(entry)
        if kept and used + entry_tokens > budget_tokens:
            break
        kept.append(entry)
        used += entry_tokens
    return "\n".join(reversed(kept)).strip()
