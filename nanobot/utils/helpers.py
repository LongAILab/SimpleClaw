"""Utility functions for nanobot."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import tiktoken


def detect_image_mime(data: bytes) -> str | None:
    """Detect image MIME type from magic bytes, ignoring file extension."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')

def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def split_message(content: str, max_len: int = 2000) -> list[str]:
    """
    Split content into chunks within max_len, preferring line breaks.

    Args:
        content: The text content to split.
        max_len: Maximum length per chunk (default 2000 for Discord compatibility).

    Returns:
        List of message chunks, each within max_len.
    """
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        # Try to break at newline first, then space, then hard break
        pos = cut.rfind('\n')
        if pos <= 0:
            pos = cut.rfind(' ')
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


def build_assistant_message(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    thinking_blocks: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a provider-safe assistant message with optional reasoning fields."""
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    if thinking_blocks:
        msg["thinking_blocks"] = thinking_blocks
    return msg


def estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate prompt tokens with tiktoken."""
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        parts: list[str] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        txt = part.get("text", "")
                        if txt:
                            parts.append(txt)
        if tools:
            parts.append(json.dumps(tools, ensure_ascii=False))
        return len(enc.encode("\n".join(parts)))
    except Exception:
        return 0


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate prompt tokens contributed by one persisted message."""
    content = message.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    parts.append(text)
            else:
                parts.append(json.dumps(part, ensure_ascii=False))
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))

    for key in ("name", "tool_call_id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if message.get("tool_calls"):
        parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))

    payload = "\n".join(parts)
    if not payload:
        return 1
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return max(1, len(enc.encode(payload)))
    except Exception:
        return max(1, len(payload) // 4)


def estimate_prompt_tokens_chain(
    provider: Any,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Estimate prompt tokens via provider counter first, then tiktoken fallback."""
    provider_counter = getattr(provider, "estimate_prompt_tokens", None)
    if callable(provider_counter):
        try:
            tokens, source = provider_counter(messages, tools, model)
            if isinstance(tokens, (int, float)) and tokens > 0:
                return int(tokens), str(source or "provider_counter")
        except Exception:
            pass

    estimated = estimate_prompt_tokens(messages, tools)
    if estimated > 0:
        return int(estimated), "tiktoken"
    return 0, "none"


_HEARTBEAT_UNIT_SECONDS: dict[str, int] = {
    "秒": 1,
    "秒钟": 1,
    "秒钟后": 1,
    "sec": 1,
    "second": 1,
    "seconds": 1,
    "分": 60,
    "分钟": 60,
    "分钟后": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "小时": 3600,
    "钟头": 3600,
    "hour": 3600,
    "hours": 3600,
    "天": 86400,
    "day": 86400,
    "days": 86400,
    "周": 7 * 86400,
    "星期": 7 * 86400,
    "week": 7 * 86400,
    "weeks": 7 * 86400,
}

_CHINESE_DIGIT_MAP: dict[str, int] = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _parse_small_chinese_number(text: str) -> float | None:
    token = text.strip()
    if not token:
        return None
    if token == "半":
        return 0.5
    if token.isdigit():
        return float(token)
    if token in _CHINESE_DIGIT_MAP:
        return float(_CHINESE_DIGIT_MAP[token])
    if token == "十":
        return 10.0
    if "十" in token:
        left, _, right = token.partition("十")
        tens = 1 if not left else _CHINESE_DIGIT_MAP.get(left)
        ones = 0 if not right else _CHINESE_DIGIT_MAP.get(right)
        if tens is None or ones is None:
            return None
        return float(tens * 10 + ones)
    return None


def infer_heartbeat_interval_s(content: str) -> int | None:
    """Infer a heartbeat interval in seconds from natural-language recurring text."""
    text = (content or "").strip()
    if not text:
        return None

    candidates: list[int] = []

    zh_pattern = re.compile(
        r"(?:每隔|每)\s*(?:(\d+|[零一二两三四五六七八九十半]+)\s*)?(秒钟后|秒钟|秒|分钟后|分钟|分|小时|钟头|天|星期|周)"
    )
    for match in zh_pattern.finditer(text):
        raw_number = (match.group(1) or "1").strip()
        unit = match.group(2)
        number = _parse_small_chinese_number(raw_number)
        unit_seconds = _HEARTBEAT_UNIT_SECONDS.get(unit)
        if number is None or unit_seconds is None:
            continue
        interval_s = int(number * unit_seconds)
        if interval_s > 0:
            candidates.append(interval_s)

    en_pattern = re.compile(
        r"\bevery\s+(\d+)?\s*(seconds?|secs?|minutes?|mins?|hours?|days?|weeks?)\b",
        flags=re.IGNORECASE,
    )
    for match in en_pattern.finditer(text):
        raw_number = (match.group(1) or "1").strip()
        unit = match.group(2).lower()
        unit_seconds = _HEARTBEAT_UNIT_SECONDS.get(unit)
        if not raw_number.isdigit() or unit_seconds is None:
            continue
        interval_s = int(raw_number) * unit_seconds
        if interval_s > 0:
            candidates.append(interval_s)

    return min(candidates) if candidates else None


def _load_template_dir():
    """Return the bundled template directory, or None if unavailable."""
    from importlib.resources import files as pkg_files

    try:
        template_dir = pkg_files("nanobot") / "templates"
    except Exception:
        return None
    if not template_dir.is_dir():
        return None
    return template_dir


def get_default_tenant_memory_markdown() -> str:
    """Return the default tenant long-term memory markdown."""
    tpl = _load_template_dir()
    if tpl is None:
        return ""
    return (tpl / "memory" / "MEMORY.md").read_text(encoding="utf-8")


def _sync_template_bundle(
    target: Path,
    *,
    include_memory: bool,
    include_root_markdown: bool,
) -> list[str]:
    """Sync bundled templates into one target directory without overwriting."""
    tpl = _load_template_dir()
    if tpl is None:
        return []

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(target)))

    if include_root_markdown:
        for item in tpl.iterdir():
            if item.name.endswith(".md") and not item.name.startswith("."):
                if item.name == "HEARTBEAT.md":
                    continue
                _write(item, target / item.name)
    if include_memory:
        _write(tpl / "memory" / "MEMORY.md", target / "memory" / "MEMORY.md")
        _write(None, target / "memory" / "HISTORY.md")
    (target / "skills").mkdir(exist_ok=True)
    return added


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to the default workspace and shared base."""
    added = _sync_template_bundle(
        workspace,
        include_memory=True,
        include_root_markdown=True,
    )

    base_workspace = workspace / "base"
    base_workspace.mkdir(parents=True, exist_ok=True)
    base_added = _sync_template_bundle(
        base_workspace,
        include_memory=False,
        include_root_markdown=True,
    )
    added.extend([f"base/{name}" for name in base_added])

    if added and not silent:
        from rich.console import Console
        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added


def sync_tenant_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Ensure tenant-private workspace directories exist without copying base prompt files."""
    tpl = _load_template_dir()
    if tpl is None:
        return []

    added: list[str] = []

    def _touch(dest: Path, content: str = "") -> None:
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    _touch(workspace / "memory" / "MEMORY.md", get_default_tenant_memory_markdown())
    _touch(workspace / "memory" / "HISTORY.md")
    for dirname in ("skills", "overrides", "sessions"):
        path = workspace / dirname
        if path.exists():
            continue
        path.mkdir(parents=True, exist_ok=True)
        added.append(str(path.relative_to(workspace)) + "/")

    if added and not silent:
        from rich.console import Console
        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added
