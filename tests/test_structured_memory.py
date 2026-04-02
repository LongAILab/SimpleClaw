from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from simpleclaw.agent.structured_memory import StructuredMemoryItem, StructuredMemoryManager
from simpleclaw.providers.base import LLMResponse


class DummyMemoryStore:
    def __init__(self, initial: str = "") -> None:
        self.content = initial
        self.history: list[str] = []

    def read_long_term(self) -> str:
        return self.content

    def write_long_term(self, content: str) -> None:
        self.content = content

    def append_history(self, entry: str) -> None:
        self.history.append(entry)


class DummyDocumentStore:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str, str], str] = {}

    def get_active_content(self, tenant_key: str, doc_type: str, doc_name: str) -> str | None:
        return self.docs.get((tenant_key, doc_type, doc_name))

    def upsert_document(
        self,
        *,
        tenant_key: str,
        doc_type: str,
        doc_name: str,
        content: str,
        **_: object,
    ) -> int:
        self.docs[(tenant_key, doc_type, doc_name)] = content
        return 1


class DummyTenantState:
    def __init__(self, *, enabled: bool = True, interval_s: int = 180) -> None:
        self.heartbeat = SimpleNamespace(enabled=enabled, interval_s=interval_s)


class DummyTenantStateRepo:
    def __init__(self, *, interval_s: int = 180) -> None:
        self.state = DummyTenantState(interval_s=interval_s)
        self.calls: list[dict[str, object]] = []

    def get(self, tenant_key: str):
        assert tenant_key == "tenant-a"
        return self.state

    def configure_heartbeat(
        self,
        tenant_key: str,
        *,
        enabled: bool | None = None,
        interval_s: int | None = None,
        next_run_at_ms: int | None = None,
    ):
        assert tenant_key == "tenant-a"
        if enabled is not None:
            self.state.heartbeat.enabled = enabled
        if interval_s is not None:
            self.state.heartbeat.interval_s = interval_s
        self.calls.append(
            {
                "enabled": enabled,
                "interval_s": interval_s,
                "next_run_at_ms": next_run_at_ms,
            }
        )
        return self.state


def make_runtime(
    *,
    memory: DummyMemoryStore | None = None,
    document_store: DummyDocumentStore | None = None,
    tenant_state_repo: DummyTenantStateRepo | None = None,
):
    return SimpleNamespace(
        tenant_key="tenant-a",
        context=SimpleNamespace(
            memory=memory or DummyMemoryStore(),
            document_store=document_store,
            tenant_state_repo=tenant_state_repo,
        ),
    )


@pytest.mark.asyncio
async def test_extract_items_supports_alias_and_heartbeat_categories() -> None:
    provider = SimpleNamespace()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content=(
                '{"should_write": true, "items": ['
                '{"category": "assistant_alias", "value": "小美"}, '
                '{"category": "preferred_address", "value": "老大"}, '
                '{"category": "recurring_reminder", "value": "每两小时提醒喝水"}'
                "]}"
            )
        )
    )
    manager = StructuredMemoryManager(provider=provider, model="test-model")

    items = await manager._extract_items(
        runtime=make_runtime(memory=DummyMemoryStore("")),
        origin_user_message="以后你叫小美，我叫老大，每两小时提醒我喝水。",
        assistant_reply="记住啦。",
        recent_messages=[],
    )

    assert [item.category for item in items] == [
        "assistant_alias",
        "preferred_address",
        "recurring_reminder",
    ]


def test_apply_items_routes_to_memory_and_tenant_documents() -> None:
    provider = SimpleNamespace(chat_with_retry=AsyncMock())
    manager = StructuredMemoryManager(provider=provider, model="test-model")
    memory = DummyMemoryStore("")
    documents = DummyDocumentStore()
    tenant_state_repo = DummyTenantStateRepo()
    runtime = make_runtime(
        memory=memory,
        document_store=documents,
        tenant_state_repo=tenant_state_repo,
    )

    changed = manager._apply_items(
        runtime,
        [
            StructuredMemoryItem("assistant_alias", "User Information", "对我的称呼", "小美"),
            StructuredMemoryItem("preferred_address", "User Information", "称呼", "老大"),
            StructuredMemoryItem("communication_style", "User Information", "沟通风格", "偏直接"),
            StructuredMemoryItem("recurring_reminder", None, None, "每两小时提醒喝水"),
        ],
    )

    assert changed is True
    assert "对我的称呼：小美" in memory.content
    assert "称呼：老大" in memory.content

    user_doc = documents.get_active_content("tenant-a", "user", "USER.md") or ""
    soul_doc = documents.get_active_content("tenant-a", "soul", "SOUL.md") or ""
    heartbeat_doc = documents.get_active_content("tenant-a", "heartbeat", "HEARTBEAT.md") or ""

    assert "## Learned User Profile" in user_doc
    assert "用户对我的称呼：小美" in user_doc
    assert "用户希望被称呼：老大" in user_doc
    assert "## Learned Soul Adjustments" in soul_doc
    assert "语言风格：偏直接" in soul_doc
    assert "## Learned Recurring Tasks" in heartbeat_doc
    assert "- 每两小时提醒喝水" in heartbeat_doc
    assert memory.history and "recurring_reminder=每两小时提醒喝水" in memory.history[-1]
    assert tenant_state_repo.calls
    assert tenant_state_repo.state.heartbeat.interval_s == 7200


def test_apply_items_with_doc_only_updates_does_not_seed_empty_memory() -> None:
    provider = SimpleNamespace(chat_with_retry=AsyncMock())
    manager = StructuredMemoryManager(provider=provider, model="test-model")
    memory = DummyMemoryStore("")
    documents = DummyDocumentStore()
    runtime = make_runtime(memory=memory, document_store=documents)

    changed = manager._apply_items(
        runtime,
        [
            StructuredMemoryItem("recurring_checkin", None, None, "每周回访一次皮肤状态"),
        ],
    )

    assert changed is True
    assert memory.content == ""
    heartbeat_doc = documents.get_active_content("tenant-a", "heartbeat", "HEARTBEAT.md") or ""
    assert "- 每周回访一次皮肤状态" in heartbeat_doc


def test_apply_items_updates_heartbeat_interval_to_fastest_task() -> None:
    provider = SimpleNamespace(chat_with_retry=AsyncMock())
    manager = StructuredMemoryManager(provider=provider, model="test-model")
    tenant_state_repo = DummyTenantStateRepo(interval_s=180)
    runtime = make_runtime(
        memory=DummyMemoryStore(""),
        document_store=DummyDocumentStore(),
        tenant_state_repo=tenant_state_repo,
    )

    changed = manager._apply_items(
        runtime,
        [
            StructuredMemoryItem("recurring_checkin", None, None, "每两小时回访一次皮肤状态"),
            StructuredMemoryItem("recurring_reminder", None, None, "每2分钟提醒喝水"),
        ],
    )

    assert changed is True
    assert tenant_state_repo.state.heartbeat.interval_s == 120
