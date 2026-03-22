"""File system tools: read, write, edit, list."""

import difflib
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.utils.helpers import infer_heartbeat_interval_s


def _resolve_path(
    path: str, workspace: Path | None = None, allowed_dir: Path | None = None
) -> Path:
    """Resolve path against workspace (if relative) and enforce directory restriction."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if allowed_dir:
        try:
            resolved.relative_to(allowed_dir.resolve())
        except ValueError:
            raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


class _FsTool(Tool):
    """Shared base for filesystem tools — common init and path resolution."""

    _DOC_TYPE_MAP = {
        "SOUL.md": "soul",
        "USER.md": "user",
        "TOOLS.md": "tools",
        "HEARTBEAT.md": "heartbeat",
    }

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
        *,
        document_store: Any | None = None,
        memory_store: Any | None = None,
        tenant_state_repo: Any | None = None,
    ):
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._document_store = document_store
        self._memory_store = memory_store
        self._tenant_state_repo = tenant_state_repo
        self._workspace_ctx: ContextVar[Path | None] = ContextVar(
            f"{self.__class__.__name__}_workspace", default=workspace
        )
        self._allowed_dir_ctx: ContextVar[Path | None] = ContextVar(
            f"{self.__class__.__name__}_allowed_dir", default=allowed_dir
        )
        self._tenant_key_ctx: ContextVar[str] = ContextVar(
            f"{self.__class__.__name__}_tenant_key", default="__default__"
        )

    def set_context(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
        tenant_key: str | None = None,
    ) -> None:
        """Override workspace/allowed_dir for the current task."""
        self._workspace_ctx.set(workspace if workspace is not None else self._workspace)
        self._allowed_dir_ctx.set(allowed_dir if allowed_dir is not None else self._allowed_dir)
        self._tenant_key_ctx.set(tenant_key or "__default__")

    def _resolve(self, path: str) -> Path:
        return _resolve_path(path, self._workspace_ctx.get(), self._allowed_dir_ctx.get())

    def _special_target(self, fp: Path) -> tuple[str, str] | None:
        parent = fp.parent.as_posix().lower()
        name = fp.name
        if parent.endswith("/overrides") and name in self._DOC_TYPE_MAP:
            return ("document", self._DOC_TYPE_MAP[name])
        if parent.endswith("/memory") and name == "MEMORY.md":
            return ("memory", "memory")
        if parent.endswith("/memory") and name == "HISTORY.md":
            return ("history", "history")
        return None

    def _read_special(self, fp: Path) -> str | None:
        target = self._special_target(fp)
        tenant_key = self._tenant_key_ctx.get()
        if target is None:
            return None
        kind, value = target
        if kind == "document" and self._document_store is not None:
            return self._document_store.get_active_content(tenant_key, value, fp.name) or ""
        if kind == "memory" and self._memory_store is not None:
            return self._memory_store.read_long_term(tenant_key)
        if kind == "history" and self._memory_store is not None:
            return self._memory_store.render_history_text(tenant_key)
        return None

    def _write_special(self, fp: Path, content: str) -> bool:
        target = self._special_target(fp)
        tenant_key = self._tenant_key_ctx.get()
        if target is None:
            return False
        kind, value = target
        if kind == "document" and self._document_store is not None:
            self._document_store.upsert_document(
                tenant_key=tenant_key,
                doc_type=value,
                doc_name=fp.name,
                content=content,
                updated_by="agent",
                change_source="agent",
            )
            if value == "heartbeat":
                interval_s = infer_heartbeat_interval_s(content)
                if interval_s is not None and self._tenant_state_repo is not None:
                    self._tenant_state_repo.configure_heartbeat(
                        tenant_key,
                        enabled=True,
                        interval_s=interval_s,
                        next_run_at_ms=int(time.time() * 1000) + interval_s * 1000,
                    )
            return True
        if kind == "memory" and self._memory_store is not None:
            self._memory_store.save_snapshot(tenant_key, long_term_markdown=content)
            return True
        if kind == "history" and self._memory_store is not None:
            self._memory_store.overwrite_history(tenant_key, content)
            return True
        return False


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

class ReadFileTool(_FsTool):
    """Read file contents with optional line-based pagination."""

    _MAX_CHARS = 128_000
    _DEFAULT_LIMIT = 2000

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read a file with numbered lines. Use offset/limit to paginate."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "offset": {
                    "type": "integer",
                    "description": "Start line (1-indexed)",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to read",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, offset: int = 1, limit: int | None = None, **kwargs: Any) -> str:
        try:
            fp = self._resolve(path)
            special = self._read_special(fp)
            if special is not None:
                all_lines = special.splitlines()
                total = len(all_lines)
                if offset < 1:
                    offset = 1
                if total == 0:
                    return f"(Empty file: {path})"
                if offset > total:
                    return f"Error: offset {offset} is beyond end of file ({total} lines)"
                start = offset - 1
                end = min(start + (limit or self._DEFAULT_LIMIT), total)
                numbered = [f"{start + i + 1}| {line}" for i, line in enumerate(all_lines[start:end])]
                result = "\n".join(numbered)
                if end < total:
                    result += f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
                else:
                    result += f"\n\n(End of file — {total} lines total)"
                return result
            if not fp.exists():
                return f"Error: File not found: {path}"
            if not fp.is_file():
                return f"Error: Not a file: {path}"

            all_lines = fp.read_text(encoding="utf-8").splitlines()
            total = len(all_lines)

            if offset < 1:
                offset = 1
            if total == 0:
                return f"(Empty file: {path})"
            if offset > total:
                return f"Error: offset {offset} is beyond end of file ({total} lines)"

            start = offset - 1
            end = min(start + (limit or self._DEFAULT_LIMIT), total)
            numbered = [f"{start + i + 1}| {line}" for i, line in enumerate(all_lines[start:end])]
            result = "\n".join(numbered)

            if len(result) > self._MAX_CHARS:
                trimmed, chars = [], 0
                for line in numbered:
                    chars += len(line) + 1
                    if chars > self._MAX_CHARS:
                        break
                    trimmed.append(line)
                end = start + len(trimmed)
                result = "\n".join(trimmed)

            if end < total:
                result += f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
            else:
                result += f"\n\n(End of file — {total} lines total)"
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

class WriteFileTool(_FsTool):
    """Write content to a file."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "File content"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            fp = self._resolve(path)
            if self._write_special(fp, content):
                return f"Successfully wrote {len(content)} bytes to database-backed {fp.name}"
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {fp}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

def _find_match(content: str, old_text: str) -> tuple[str | None, int]:
    """Locate old_text in content: exact first, then line-trimmed sliding window.

    Both inputs should use LF line endings (caller normalises CRLF).
    Returns (matched_fragment, count) or (None, 0).
    """
    if old_text in content:
        return old_text, content.count(old_text)

    old_lines = old_text.splitlines()
    if not old_lines:
        return None, 0
    stripped_old = [l.strip() for l in old_lines]
    content_lines = content.splitlines()

    candidates = []
    for i in range(len(content_lines) - len(stripped_old) + 1):
        window = content_lines[i : i + len(stripped_old)]
        if [l.strip() for l in window] == stripped_old:
            candidates.append("\n".join(window))

    if candidates:
        return candidates[0], len(candidates)
    return None, 0


class EditFileTool(_FsTool):
    """Edit a file by replacing text with fallback matching."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Replace old_text with new_text in a file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "old_text": {"type": "string", "description": "Text to replace"},
                "new_text": {"type": "string", "description": "Replacement text"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all matches",
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(
        self, path: str, old_text: str, new_text: str,
        replace_all: bool = False, **kwargs: Any,
    ) -> str:
        try:
            fp = self._resolve(path)
            special = self._read_special(fp)
            if special is not None:
                content = special.replace("\r\n", "\n")
                match, count = _find_match(content, old_text.replace("\r\n", "\n"))
                if match is None:
                    return self._not_found_msg(old_text, content, path)
                if count > 1 and not replace_all:
                    return (
                        f"Warning: old_text appears {count} times. "
                        "Provide more context to make it unique, or set replace_all=true."
                    )
                norm_new = new_text.replace("\r\n", "\n")
                new_content = content.replace(match, norm_new) if replace_all else content.replace(match, norm_new, 1)
                if self._write_special(fp, new_content):
                    return f"Successfully edited database-backed {fp.name}"
                return f"Error editing special file: {path}"
            if not fp.exists():
                return f"Error: File not found: {path}"

            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")
            match, count = _find_match(content, old_text.replace("\r\n", "\n"))

            if match is None:
                return self._not_found_msg(old_text, content, path)
            if count > 1 and not replace_all:
                return (
                    f"Warning: old_text appears {count} times. "
                    "Provide more context to make it unique, or set replace_all=true."
                )

            norm_new = new_text.replace("\r\n", "\n")
            new_content = content.replace(match, norm_new) if replace_all else content.replace(match, norm_new, 1)
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")

            fp.write_bytes(new_content.encode("utf-8"))
            return f"Successfully edited {fp}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {e}"

    @staticmethod
    def _not_found_msg(old_text: str, content: str, path: str) -> str:
        lines = content.splitlines(keepends=True)
        old_lines = old_text.splitlines(keepends=True)
        window = len(old_lines)

        best_ratio, best_start = 0.0, 0
        for i in range(max(1, len(lines) - window + 1)):
            ratio = difflib.SequenceMatcher(None, old_lines, lines[i : i + window]).ratio()
            if ratio > best_ratio:
                best_ratio, best_start = ratio, i

        if best_ratio > 0.5:
            diff = "\n".join(difflib.unified_diff(
                old_lines, lines[best_start : best_start + window],
                fromfile="old_text (provided)",
                tofile=f"{path} (actual, line {best_start + 1})",
                lineterm="",
            ))
            return f"Error: old_text not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
        return f"Error: old_text not found in {path}. No similar text found. Verify the file content."


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------

class ListDirTool(_FsTool):
    """List directory contents with optional recursion."""

    _DEFAULT_MAX = 200
    _IGNORE_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".coverage", "htmlcov",
    }

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List a directory. Use recursive=true for nested entries."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"},
                "recursive": {
                    "type": "boolean",
                    "description": "List nested entries",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Max entries",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self, path: str, recursive: bool = False,
        max_entries: int | None = None, **kwargs: Any,
    ) -> str:
        try:
            dp = self._resolve(path)
            if not dp.exists():
                return f"Error: Directory not found: {path}"
            if not dp.is_dir():
                return f"Error: Not a directory: {path}"

            cap = max_entries or self._DEFAULT_MAX
            items: list[str] = []
            total = 0

            if recursive:
                for item in sorted(dp.rglob("*")):
                    if any(p in self._IGNORE_DIRS for p in item.parts):
                        continue
                    total += 1
                    if len(items) < cap:
                        rel = item.relative_to(dp)
                        items.append(f"{rel}/" if item.is_dir() else str(rel))
            else:
                for item in sorted(dp.iterdir()):
                    if item.name in self._IGNORE_DIRS:
                        continue
                    total += 1
                    if len(items) < cap:
                        pfx = "📁 " if item.is_dir() else "📄 "
                        items.append(f"{pfx}{item.name}")

            if not items and total == 0:
                return f"Directory {path} is empty"

            result = "\n".join(items)
            if total > cap:
                result += f"\n\n(truncated, showing first {cap} of {total} entries)"
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {e}"
