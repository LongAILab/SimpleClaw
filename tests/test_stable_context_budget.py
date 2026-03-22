from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.context_budget import derive_context_budget_tokens
from nanobot.agent.memory import MemoryConsolidator
from nanobot.session.manager import Session, SessionManager


class _MemoryRepo:
    def __init__(self, text: str):
        self.text = text

    def read_long_term(self, tenant_key: str) -> str:
        return self.text


class _DummyStore:
    def __init__(self, entry: str):
        self.entry = entry

    async def consolidate(self, messages, provider, model) -> bool:
        return True

    def consume_last_archive_entry(self) -> str:
        return self.entry


def test_context_budget_caps_large_context_window() -> None:
    budget = derive_context_budget_tokens(65_536)

    assert budget.soft_limit_tokens == 6000
    assert budget.target_tokens == 4500
    assert budget.recent_history_tokens == 1536


def test_session_recent_history_keeps_latest_complete_turn() -> None:
    session = Session(
        key="api:main",
        messages=[
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2 " + "x" * 300},
            {"role": "assistant", "content": "a2 " + "y" * 300},
            {"role": "user", "content": "u3"},
            {"role": "assistant", "content": "a3"},
        ],
    )

    history = session.get_recent_history(max_tokens=64)

    assert [item["content"] for item in history] == ["u3", "a3"]


def test_context_builder_compacts_memory_and_injects_session_summary(tmp_path: Path) -> None:
    builder = ContextBuilder(
        tmp_path,
        memory_store=_MemoryRepo("# Memory\n\n" + "\n".join(f"- fact {i}" for i in range(80))),
        context_window_tokens=400,
    )

    prompt = builder.build_system_prompt(
        session_metadata={"rolling_summary": "- older item\n- newer item"}
    )
    diagnostics = builder.describe_prompt_state(
        history=[{"role": "user", "content": "hi"}],
        raw_history=[{"role": "user", "content": "hi"}],
        session_metadata={"rolling_summary": "- older item\n- newer item"},
    )

    assert "# Session Summary" in prompt
    assert "# Memory" in prompt
    assert diagnostics["session_summary"]["present"] is True
    assert diagnostics["memory"]["compacted"] is True


@pytest.mark.asyncio
async def test_consolidator_updates_rolling_summary_metadata(tmp_path: Path) -> None:
    provider = MagicMock()
    sessions = SessionManager(tmp_path)
    builder = ContextBuilder(tmp_path, context_window_tokens=400)
    consolidator = MemoryConsolidator(
        workspace=tmp_path,
        provider=provider,
        model="test-model",
        sessions=sessions,
        context_window_tokens=400,
        build_messages=builder.build_messages,
        get_tool_definitions=lambda: [],
        memory_store=_DummyStore("[2026-03-22 10:00] user confirmed a new stable preference"),
    )
    session = sessions.get_or_create("api:main")
    session.metadata = {"rolling_summary": "- existing summary"}

    ok = await consolidator.consolidate_messages(
        [{"role": "user", "content": "hello"}],
        session=session,
    )

    assert ok is True
    assert "existing summary" in session.metadata["rolling_summary"]
    assert "stable preference" in session.metadata["rolling_summary"]
