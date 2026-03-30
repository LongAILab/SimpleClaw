"""Structured stable-memory extraction and deterministic writing."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

import json_repair
from loguru import logger

from nanobot.agent.runtime import TenantRuntime
from nanobot.providers.base import LLMProvider
from nanobot.utils.helpers import infer_heartbeat_interval_s


@dataclass(frozen=True, slots=True)
class StructuredMemoryItem:
    """One normalized stable-memory item."""

    category: str
    section: str | None
    label: str | None
    value: str


_CATEGORY_MAP: dict[str, tuple[str | None, str | None]] = {
    "preferred_name": (None, None),
    "preferred_address": (None, None),
    "assistant_alias": (None, None),
    "relationship_preference": (None, None),
    "primary_language": (None, None),
    "timezone": (None, None),
    "communication_style": (None, None),
    "response_length_preference": (None, None),
    "skin_type": (None, None),
    "skin_concern": (None, None),
    "makeup_style": (None, None),
    "product_preference": (None, None),
    "ingredient_avoidance": (None, None),
    "stable_beauty_preference": (None, None),
    "support_preference": (None, None),
    "decision_style": (None, None),
    "sensitive_topic": (None, None),
    "stable_constraint": ("Important Notes", "长期约束"),
    "support_boundary": (None, None),
    "daily_rhythm": ("Project Context", "作息节奏"),
    "long_term_goal": ("Project Context", "长期目标"),
    "recurring_reminder": (None, None),
    "recurring_checkin": (None, None),
    "recurring_followup": (None, None),
}

_USER_DOC_LABELS: dict[str, str] = {
    "preferred_name": "用户姓名",
    "preferred_address": "用户希望被称呼",
    "assistant_alias": "用户对我的称呼",
    "relationship_preference": "关系设定",
    "primary_language": "主要语言",
    "timezone": "时区",
    "communication_style": "沟通风格",
    "response_length_preference": "回复长度偏好",
    "skin_type": "肤质",
    "skin_concern": "主要肤况",
    "makeup_style": "妆容风格",
    "product_preference": "产品偏好",
    "ingredient_avoidance": "成分禁忌",
    "stable_beauty_preference": "长期美妆偏好",
    "support_preference": "支持偏好",
    "decision_style": "决策偏好",
    "sensitive_topic": "敏感话题",
    "support_boundary": "陪伴边界",
}

_SOUL_DOC_LABELS: dict[str, str] = {
    "relationship_preference": "关系风格",
    "communication_style": "语言风格",
    "response_length_preference": "回复节奏",
    "support_preference": "支持方式",
    "decision_style": "建议方式",
    "sensitive_topic": "温柔处理",
    "support_boundary": "陪伴边界",
}

_HEARTBEAT_CATEGORIES = {
    "recurring_reminder",
    "recurring_checkin",
    "recurring_followup",
}

_CATEGORY_GROUP_GUIDE = "\n".join(
    [
        "- 身份称呼: preferred_name, preferred_address, assistant_alias, relationship_preference, primary_language, timezone",
        "- 沟通支持: communication_style, response_length_preference, support_preference, decision_style, sensitive_topic, support_boundary",
        "- 美妆画像: skin_type, skin_concern, makeup_style, product_preference, ingredient_avoidance, stable_beauty_preference",
        "- 长期信息: stable_constraint, daily_rhythm, long_term_goal",
        "- 周期任务: recurring_reminder, recurring_checkin, recurring_followup",
    ]
)

_SECTION_PLACEHOLDERS: dict[str, str] = {
    "User Information": "(Important facts about the user)",
    "Preferences": "(User preferences learned over time)",
    "Project Context": "(Information about ongoing projects)",
    "Important Notes": "(Things to remember)",
}

_SUPPORTED_CATEGORIES = ", ".join(sorted(_CATEGORY_MAP))
_USER_DOC_SECTION = "## Learned User Profile"
_SOUL_DOC_SECTION = "## Learned Soul Adjustments"
_HEARTBEAT_DOC_SECTION = "## Learned Recurring Tasks"

StructuredMemoryRunner = Callable[
    [TenantRuntime, str, str, str, str, list[dict[str, Any]], bool, bool],
    Awaitable[None],
]


class StructuredMemoryManager:
    """Background extractor for durable user memory that did not trigger a direct file write."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        model: str,
        enabled: bool = True,
        max_concurrency: int = 1,
        recent_message_limit: int = 6,
        runner: StructuredMemoryRunner | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._enabled = enabled
        self._recent_message_limit = max(2, recent_message_limit)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._tasks: set[asyncio.Task[None]] = set()
        self._runner = runner

    @property
    def enabled(self) -> bool:
        """Whether structured stable-memory extraction is enabled."""
        return self._enabled

    def disable(self) -> None:
        """Disable scheduling for specialized background loops."""
        self._enabled = False

    def set_runner(self, runner: StructuredMemoryRunner | None) -> None:
        """Set or replace the background structured-memory runner."""
        self._runner = runner

    def schedule(
        self,
        *,
        runtime: TenantRuntime,
        channel: str,
        chat_id: str,
        session_key: str,
        origin_user_message: str,
        assistant_reply: str,
        recent_messages: list[dict[str, Any]],
        debug_trace: bool = False,
        skip_memory_items: bool = False,
    ) -> None:
        """Queue one stable-memory extraction task."""
        if not self._enabled:
            return
        if not origin_user_message.strip():
            return
        task = asyncio.create_task(
            self._dispatch(
                runtime=runtime,
                channel=channel,
                chat_id=chat_id,
                session_key=session_key,
                origin_user_message=origin_user_message,
                assistant_reply=assistant_reply,
                recent_messages=recent_messages[-self._recent_message_limit :],
                debug_trace=debug_trace,
                skip_memory_items=skip_memory_items,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch(
        self,
        *,
        runtime: TenantRuntime,
        channel: str,
        chat_id: str,
        session_key: str,
        origin_user_message: str,
        assistant_reply: str,
        recent_messages: list[dict[str, Any]],
        debug_trace: bool,
        skip_memory_items: bool,
    ) -> None:
        if self._runner is not None:
            await self._runner(
                runtime=runtime,
                channel=channel,
                chat_id=chat_id,
                session_key=session_key,
                origin_user_message=origin_user_message,
                assistant_reply=assistant_reply,
                recent_messages=recent_messages,
                debug_trace=debug_trace,
                skip_memory_items=skip_memory_items,
            )
            return
        await self.execute(
            runtime=runtime,
            session_key=session_key,
            origin_user_message=origin_user_message,
            assistant_reply=assistant_reply,
            recent_messages=recent_messages,
            skip_memory_items=skip_memory_items,
        )

    async def execute(
        self,
        *,
        runtime: TenantRuntime,
        session_key: str,
        origin_user_message: str,
        assistant_reply: str,
        recent_messages: list[dict[str, Any]],
        skip_memory_items: bool = False,
    ) -> None:
        """Execute one structured-memory extraction immediately."""
        async with self._semaphore:
            try:
                items = await self._extract_items(
                    runtime=runtime,
                    origin_user_message=origin_user_message,
                    assistant_reply=assistant_reply,
                    recent_messages=recent_messages,
                )
                if not items:
                    return
                changed = self._apply_items(runtime, items, skip_memory_items=skip_memory_items)
                if changed:
                    logger.info(
                        "Structured memory updated for {} with {} item(s)",
                        session_key,
                        len(items),
                    )
            except Exception:
                logger.exception("Structured memory extraction failed for {}", session_key)

    async def _extract_items(
        self,
        *,
        runtime: TenantRuntime,
        origin_user_message: str,
        assistant_reply: str,
        recent_messages: list[dict[str, Any]],
    ) -> list[StructuredMemoryItem]:
        current_memory = runtime.context.memory.read_long_term()
        prompt = (
            "你是一个长期信息提取器，负责从这轮对话里提取适合持久化的稳定事实或周期任务。\n"
            "这些结果会由代码分流到 `MEMORY.md`、租户 `USER.md`、租户 `SOUL.md`、租户 `HEARTBEAT.md`。\n"
            "只返回严格 JSON，不要 markdown，不要解释，不要代码块。\n\n"
            "输出格式：\n"
            "{\n"
            '  "should_write": true,\n'
            '  "items": [\n'
            "    {\n"
            f'      "category": "从以下枚举中选择一个：{_SUPPORTED_CATEGORIES}",\n'
            '      "value": "归一化后的记忆值"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "类别分组：\n"
            f"{_CATEGORY_GROUP_GUIDE}\n\n"
            "强规则：\n"
            "- 只提取未来多轮仍有价值的长期事实；临时状态、寒暄、一次性任务不要写。\n"
            "- “以后叫我xxx / 我希望你怎么称呼我” => preferred_address；“我叫xxx” => preferred_name。\n"
            "- “以后我叫你xxx / 以后你就叫xxx” => assistant_alias。\n"
            "- 沟通偏好、支持方式、护肤画像、长期目标、长期约束，都是可提取范围。\n"
            "- 只有轻量的周期任务才允许写成 recurring_*；精确时间点或严格 schedule 不要提取，交给 cron。\n"
            "- 如果当前 memory 已经清楚包含同一事实，或最近对话里只是重复确认，就不要写。\n"
            "- 值要归一化、简洁，避免口语碎片。\n"
            "- 最多返回 6 条。\n\n"
            "示例：\n"
            '- 用户说：“以后叫我老大。” => {"should_write": true, "items": [{"category": "preferred_address", "value": "老大"}]}\n'
            '- 用户说：“以后你就叫小美吧。” => {"should_write": true, "items": [{"category": "assistant_alias", "value": "小美"}]}\n'
            '- 用户说：“以后你叫小美，我叫老大。” => {"should_write": true, "items": [{"category": "assistant_alias", "value": "小美"}, {"category": "preferred_address", "value": "老大"}]}\n'
            '- 用户说：“我是敏感肌，最近尽量别给我推荐酸类。” => {"should_write": true, "items": [{"category": "skin_type", "value": "敏感肌"}, {"category": "ingredient_avoidance", "value": "尽量避免酸类"}]}\n'
            '- 用户说：“给我建议时直接一点，结论先说。” => {"should_write": true, "items": [{"category": "communication_style", "value": "偏直接，结论先说"}, {"category": "decision_style", "value": "推荐优先"}]}\n'
            '- 用户说：“以后每两小时提醒我喝水。” => {"should_write": true, "items": [{"category": "recurring_reminder", "value": "每两小时提醒喝水"}]}\n'
            '- 用户说：“今天有点困。” => {"should_write": false, "items": []}\n\n'
            "## Current MEMORY.md\n"
            f"{current_memory or '(empty)'}\n\n"
            "## Recent Conversation\n"
            f"{self._format_messages(recent_messages)}\n\n"
            "## Current User Message\n"
            f"{origin_user_message}\n\n"
            "## Already Sent Assistant Reply\n"
            f"{assistant_reply}\n"
        )
        response = await self._provider.chat_with_retry(
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise memory-intent extractor. Output strict JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            model=self._model,
            tools=None,
            max_tokens=600,
            temperature=0.0,
        )
        payload = self._parse_json(response.content)
        if not payload or not payload.get("should_write"):
            return []
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return []
        normalized: list[StructuredMemoryItem] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            category = str(raw.get("category") or "").strip()
            value = str(raw.get("value") or "").strip()
            if not category or not value:
                continue
            mapped = _CATEGORY_MAP.get(category)
            if mapped is None:
                continue
            section, label = mapped
            key = (category, value)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                StructuredMemoryItem(
                    category=category,
                    section=section,
                    label=label,
                    value=value,
                )
            )
        return normalized

    @staticmethod
    def _parse_json(content: str | None) -> dict[str, Any] | None:
        if not content:
            return None
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
        try:
            repaired = json_repair.repair_json(text, return_objects=True)
            return repaired if isinstance(repaired, dict) else None
        except Exception:
            logger.warning("Structured memory: failed to parse JSON: {}", text[:200])
            return None

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text"):
                        parts.append(str(item["text"]))
                    elif item.get("type") == "image_url":
                        parts.append("[image]")
            return " ".join(parts)
        return str(content or "")

    def _format_messages(self, messages: list[dict[str, Any]]) -> str:
        rows: list[str] = []
        for message in messages:
            role = str(message.get("role") or "unknown").upper()
            text = self._content_to_text(message.get("content")).strip()
            if text:
                rows.append(f"{role}: {text}")
        return "\n".join(rows) if rows else "(none)"

    def _apply_items(
        self,
        runtime: TenantRuntime,
        items: list[StructuredMemoryItem],
        *,
        skip_memory_items: bool = False,
    ) -> bool:
        changed = False
        if not skip_memory_items and self._apply_memory_items(runtime, items):
            changed = True
        if self._apply_document_items(runtime, items):
            changed = True
        if self._sync_heartbeat_interval(runtime, items):
            changed = True
        if changed:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            entry = "; ".join(f"{item.category}={item.value}" for item in items)
            runtime.context.memory.append_history(f"[{stamp}] [STRUCTURED] {entry}")
        return changed

    def _apply_memory_items(self, runtime: TenantRuntime, items: list[StructuredMemoryItem]) -> bool:
        store = runtime.context.memory
        memory_items = [item for item in items if item.section]
        if not memory_items:
            return False
        original = store.read_long_term()
        updated = original or self._initial_memory_text()
        for section in _SECTION_PLACEHOLDERS:
            section_items = [item for item in memory_items if item.section == section]
            if not section_items:
                continue
            updated = self._upsert_section(updated, section, section_items)
        if updated == original:
            return False
        store.write_long_term(updated)
        return True

    def _apply_document_items(self, runtime: TenantRuntime, items: list[StructuredMemoryItem]) -> bool:
        document_store = getattr(runtime.context, "document_store", None)
        if document_store is None:
            return False

        changed = False
        changed |= self._upsert_document_block(
            document_store=document_store,
            tenant_key=runtime.tenant_key,
            doc_type="user",
            doc_name="USER.md",
            heading=_USER_DOC_SECTION,
            body=self._render_key_value_body(items, _USER_DOC_LABELS),
        )
        changed |= self._upsert_document_block(
            document_store=document_store,
            tenant_key=runtime.tenant_key,
            doc_type="soul",
            doc_name="SOUL.md",
            heading=_SOUL_DOC_SECTION,
            body=self._render_key_value_body(items, _SOUL_DOC_LABELS),
        )
        changed |= self._upsert_document_block(
            document_store=document_store,
            tenant_key=runtime.tenant_key,
            doc_type="heartbeat",
            doc_name="HEARTBEAT.md",
            heading=_HEARTBEAT_DOC_SECTION,
            body=self._render_heartbeat_body(items),
        )
        return changed

    @staticmethod
    def _initial_memory_text() -> str:
        return (
            "## User Information\n\n"
            "(Important facts about the user)\n\n"
            "## Preferences\n\n"
            "(User preferences learned over time)\n\n"
            "## Project Context\n\n"
            "(Information about ongoing projects)\n\n"
            "## Important Notes\n\n"
            "(Things to remember)\n"
        )

    @staticmethod
    def _render_key_value_body(items: list[StructuredMemoryItem], labels: dict[str, str]) -> str:
        rows: dict[str, str] = {}
        for item in items:
            label = labels.get(item.category)
            if label and item.value.strip():
                rows[label] = item.value.strip()
        return "\n".join(f"- {label}：{value}" for label, value in rows.items())

    @staticmethod
    def _render_heartbeat_body(items: list[StructuredMemoryItem]) -> str:
        seen: set[str] = set()
        lines: list[str] = []
        for item in items:
            if item.category not in _HEARTBEAT_CATEGORIES:
                continue
            value = item.value.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            lines.append(f"- {value}")
        return "\n".join(lines)

    def _upsert_document_block(
        self,
        *,
        document_store: Any,
        tenant_key: str,
        doc_type: str,
        doc_name: str,
        heading: str,
        body: str,
    ) -> bool:
        getter = getattr(document_store, "get_active_content", None)
        upsert = getattr(document_store, "upsert_document", None)
        if not callable(getter) or not callable(upsert):
            return False

        existing = getter(tenant_key, doc_type, doc_name) or ""
        updated = self._merge_managed_section(existing, heading, body)
        if updated == existing:
            return False
        upsert(
            tenant_key=tenant_key,
            doc_type=doc_type,
            doc_name=doc_name,
            content=updated,
            updated_by="structured_memory",
            change_source="structured_memory",
            change_summary=f"Update {doc_name} learned section",
        )
        return True

    @staticmethod
    def _sync_heartbeat_interval(runtime: TenantRuntime, items: list[StructuredMemoryItem]) -> bool:
        tenant_state_repo = getattr(runtime.context, "tenant_state_repo", None)
        if tenant_state_repo is None:
            return False

        intervals = [
            infer_heartbeat_interval_s(item.value)
            for item in items
            if item.category in _HEARTBEAT_CATEGORIES and item.value.strip()
        ]
        intervals = [interval for interval in intervals if interval is not None]
        if not intervals:
            return False

        interval_s = min(intervals)
        current = tenant_state_repo.get(runtime.tenant_key)
        current_interval = None if current is None else int(current.heartbeat.interval_s)
        if current is not None and current.heartbeat.enabled and current_interval == interval_s:
            return False

        tenant_state_repo.configure_heartbeat(
            runtime.tenant_key,
            enabled=True,
            interval_s=interval_s,
            next_run_at_ms=int(datetime.now().timestamp() * 1000) + interval_s * 1000,
        )
        return True

    @staticmethod
    def _merge_managed_section(content: str, heading: str, body: str) -> str:
        original = content.strip()
        pattern = rf"(?:\n|^){re.escape(heading)}\n\n.*?(?=(?:\n## )|\n---\n|$)"

        if not body.strip():
            if not original:
                return ""
            cleaned = re.sub(pattern, "", original, count=1, flags=re.S).strip()
            return re.sub(r"\n{3,}", "\n\n", cleaned)

        block = f"{heading}\n\n{body.strip()}"
        if not original:
            return block

        replace_pattern = rf"({re.escape(heading)}\n\n)(.*?)(?=(?:\n## )|\n---\n|$)"
        if re.search(replace_pattern, original, flags=re.S):
            updated = re.sub(
                replace_pattern,
                lambda match: f"{match.group(1)}{body.strip()}",
                original,
                count=1,
                flags=re.S,
            )
            return updated.strip()
        return f"{original}\n\n{block}".strip()

    def _upsert_section(
        self,
        text: str,
        section: str,
        items: list[StructuredMemoryItem],
    ) -> str:
        pattern = rf"(## {re.escape(section)}\n\n)(.*?)(?=\n## |\n---\n|$)"
        match = re.search(pattern, text, flags=re.S)
        if not match:
            return text
        body = match.group(2)
        lines = body.splitlines()
        placeholder = _SECTION_PLACEHOLDERS[section]
        cleaned: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped == placeholder:
                continue
            if any(item.label and self._matches_label_line(stripped, item.label) for item in items):
                continue
            cleaned.append(line.rstrip())
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        if cleaned and cleaned[-1].strip():
            cleaned.append("")
        cleaned.extend(f"- {item.label}：{item.value}" for item in items if item.label)
        new_body = "\n".join(cleaned).rstrip() or placeholder
        return re.sub(pattern, lambda match: f"{match.group(1)}{new_body}\n", text, count=1, flags=re.S)

    @staticmethod
    def _matches_label_line(line: str, label: str) -> bool:
        pattern = rf"^- (?:\*\*)?{re.escape(label)}(?:\*\*)?[:：]\s*.*$"
        return re.match(pattern, line) is not None
