"""Tests for cache-friendly prompt construction."""

from __future__ import annotations

from datetime import datetime as real_datetime
from importlib.resources import files as pkg_files
from pathlib import Path
import datetime as datetime_module

from simpleclaw.agent.context import ContextBuilder
from simpleclaw.agent.skills import SkillsLoader
from simpleclaw.utils.helpers import sync_tenant_workspace_templates, sync_workspace_templates


class _FakeDatetime(real_datetime):
    current = real_datetime(2026, 2, 24, 13, 59)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


def test_bootstrap_files_are_backed_by_templates() -> None:
    template_dir = pkg_files("simpleclaw") / "templates"

    for filename in ContextBuilder.SHARED_BOOTSTRAP_FILES:
        assert (template_dir / filename).is_file(), f"missing bootstrap template: {filename}"


def test_system_prompt_stays_stable_when_clock_changes(tmp_path, monkeypatch) -> None:
    """System prompt should not change just because wall clock minute changes."""
    monkeypatch.setattr(datetime_module, "datetime", _FakeDatetime)

    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    _FakeDatetime.current = real_datetime(2026, 2, 24, 13, 59)
    prompt1 = builder.build_system_prompt()

    _FakeDatetime.current = real_datetime(2026, 2, 24, 14, 0)
    prompt2 = builder.build_system_prompt()

    assert prompt1 == prompt2


def test_runtime_context_is_separate_untrusted_user_message(tmp_path) -> None:
    """Runtime metadata should be merged with the user message."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
    )

    assert messages[0]["role"] == "system"
    assert "## Current Session" not in messages[0]["content"]

    # Runtime context is now merged with user message into a single message
    assert messages[-1]["role"] == "user"
    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert ContextBuilder._RUNTIME_CONTEXT_TAG in user_content
    assert "Current Time:" in user_content
    assert "Channel: cli" in user_content
    assert "Chat ID: direct" in user_content
    assert "Return exactly: OK" in user_content


def test_tenant_prompt_uses_shared_base(tmp_path) -> None:
    """Shared base files are included in the stable prompt prefix."""
    workspace = _make_workspace(tmp_path)
    sync_workspace_templates(workspace, silent=True)

    tenant_workspace = workspace / "tenants" / "tenant-a"
    (workspace / "base" / "AGENTS.md").write_text("Base rule", encoding="utf-8")

    prompt = ContextBuilder(tenant_workspace).build_system_prompt()

    assert "Base rule" in prompt
    assert "# Shared Base" in prompt


def test_tenant_mysql_soul_goes_to_stable_user_goes_to_dynamic(tmp_path) -> None:
    """Mutable MySQL tenant docs stay in the dynamic tail."""

    class _FakeDocStore:
        def get_active_content(self, tenant_key: str, doc_type: str, doc_name: str) -> str | None:
            if doc_type == "soul":
                return "Soul config content"
            if doc_type == "user":
                return "User profile content"
            return None

    workspace = _make_workspace(tmp_path)
    tenant_workspace = workspace / "tenants" / "tenant-a"
    builder = ContextBuilder(tenant_workspace, document_store=_FakeDocStore(), tenant_key="tenant-a")
    sections = builder.build_system_prompt_sections()

    stable = sections["stable_prompt"]
    dynamic = sections["dynamic_prompt"]

    assert "Soul config content" not in stable
    assert "User profile content" not in stable
    assert "User profile content" in dynamic
    assert "Soul config content" in dynamic


def test_skills_loader_prefers_tenant_then_shared_then_builtin(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    sync_workspace_templates(workspace, silent=True)

    tenant_workspace = workspace / "tenants" / "tenant-a"
    sync_tenant_workspace_templates(tenant_workspace, silent=True)

    shared_skill = workspace / "base" / "skills" / "shared-only"
    shared_skill.mkdir(parents=True)
    (shared_skill / "SKILL.md").write_text("shared skill body", encoding="utf-8")

    shadowed_shared = workspace / "base" / "skills" / "shadowed"
    shadowed_shared.mkdir(parents=True)
    (shadowed_shared / "SKILL.md").write_text("shared shadowed body", encoding="utf-8")

    tenant_skill = tenant_workspace / "skills" / "shadowed"
    tenant_skill.mkdir(parents=True)
    (tenant_skill / "SKILL.md").write_text("tenant shadowed body", encoding="utf-8")

    loader = SkillsLoader(tenant_workspace)
    summary = loader.build_skills_summary()
    workspace_summary = loader.build_skills_summary(source_filter="workspace")
    shared_summary = loader.build_skills_summary(source_filter="shared")

    assert loader.load_skill("shadowed") == "tenant shadowed body"
    assert loader.load_skill("shared-only") == "shared skill body"
    assert "shared-only" in summary
    assert "shadowed" in workspace_summary
    assert "shared-only" not in workspace_summary
    assert "shared-only" in shared_summary
    assert "shadowed" not in shared_summary


def test_sync_workspace_templates_skips_heartbeat_markdown(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    sync_workspace_templates(workspace, silent=True)

    assert not (workspace / "HEARTBEAT.md").exists()
    assert not (workspace / "base" / "HEARTBEAT.md").exists()


def test_system_prompt_puts_stable_sections_before_summary_and_memory(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    builder._get_identity = lambda: "# Identity"  # type: ignore[method-assign]
    # New signature: (shared, tenant_dynamic)
    builder._load_bootstrap_layers = lambda: (  # type: ignore[method-assign]
        "## AGENTS.md\n\nShared Bootstrap",
        "## SOUL.md\n\nTenant Soul\n\n## USER.md\n\nTenant User",
    )
    builder._compact_session_summary = lambda metadata: ("summary block", {})  # type: ignore[method-assign]
    builder._compact_memory_context = lambda memory: ("memory block", {})  # type: ignore[method-assign]
    builder.memory.get_memory_context = lambda: "raw memory"  # type: ignore[method-assign]
    builder.skills.get_always_skills = lambda source_filter=None: ["always-skill"] if source_filter else []  # type: ignore[method-assign]
    builder.skills.load_skills_for_context = lambda names: "always skill body"  # type: ignore[method-assign]
    builder.skills.build_skills_summary = lambda source_filter=None: "- `skill-a`: desc" if source_filter else ""  # type: ignore[method-assign]

    prompt = builder.build_system_prompt(extra_sections=["# Extra"], session_metadata={"rolling_summary": "x"})

    identity_idx = prompt.index("# Identity")
    shared_base_idx = prompt.index("# Shared Base")
    shared_skills_idx = prompt.index("# Shared Skills")
    tenant_workspace_idx = prompt.index("# Tenant Workspace")
    tenant_skills_idx = prompt.index("# Tenant Skills")
    summary_idx = prompt.index("# Session Summary")
    memory_idx = prompt.index("# Memory")
    extra_idx = prompt.index("# Extra")

    # Stable sections come before dynamic sections
    assert identity_idx < shared_base_idx < shared_skills_idx
    assert shared_skills_idx < tenant_workspace_idx < tenant_skills_idx < summary_idx < memory_idx < extra_idx


def test_system_prompt_sections_split_stable_prefix_and_dynamic_tail(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    builder._get_identity = lambda: "# Identity"  # type: ignore[method-assign]
    # New signature: (shared, tenant_dynamic)
    builder._load_bootstrap_layers = lambda: (  # type: ignore[method-assign]
        "shared bootstrap",
        "tenant workspace profile",
    )
    builder._build_skill_layer = lambda *, layer_name, source_filter: f"# {layer_name}\n\n{source_filter}"  # type: ignore[method-assign]
    builder._compact_session_summary = lambda metadata: ("summary block", {})  # type: ignore[method-assign]
    builder._compact_memory_context = lambda memory: ("memory block", {})  # type: ignore[method-assign]
    builder.memory.get_memory_context = lambda: "raw memory"  # type: ignore[method-assign]

    sections = builder.build_system_prompt_sections(
        extra_sections=["# Extra"],
        session_metadata={"rolling_summary": "x"},
    )

    # Stable: identity, shared base, shared skills only
    assert "# Identity" in sections["stable_prompt"]
    assert "# Shared Base" in sections["stable_prompt"]
    assert "# Shared Skills" in sections["stable_prompt"]
    assert "# Tenant Workspace" not in sections["stable_prompt"]
    assert "# Tenant Skills" not in sections["stable_prompt"]
    assert "# Session Summary" not in sections["stable_prompt"]
    assert "# Memory" not in sections["stable_prompt"]

    # Dynamic: tenant docs, tenant skills, session summary, memory, extra
    assert "# Tenant Workspace" in sections["dynamic_prompt"]
    assert "# Tenant Skills" in sections["dynamic_prompt"]
    assert "# Session Summary" in sections["dynamic_prompt"]
    assert "# Memory" in sections["dynamic_prompt"]
    assert "# Extra" in sections["dynamic_prompt"]

    assert sections["full_prompt"] == "\n\n---\n\n".join(
        sections["stable_parts"] + sections["dynamic_parts"]
    )
