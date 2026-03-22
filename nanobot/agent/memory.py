"""Memory system for persistent agent memory."""

from __future__ import annotations

import asyncio
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.agent.context_budget import ContextBudget, derive_context_budget_tokens
from nanobot.agent.memory_store import MemoryStore
from nanobot.agent.session_summary import (
    normalize_summary_entry,
    is_background_summary_chunk,
    is_noise_session_summary_entry,
    merge_rolling_summary,
)
from nanobot.utils.helpers import estimate_message_tokens, estimate_prompt_tokens_chain

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session, SessionManager


class MemoryConsolidator:
    """Owns consolidation policy, locking, and session offset updates."""

    _MAX_CONSOLIDATION_ROUNDS = 5
    _MAX_SUMMARY_ENTRIES = 4

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        memory_store: MemoryStore | None = None,
    ):
        self.store = memory_store or MemoryStore(workspace)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    @property
    def budget(self) -> ContextBudget:
        """Stable internal budget derived from the configured context window."""
        return derive_context_budget_tokens(self.context_window_tokens)

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    @staticmethod
    def _normalize_summary_entry(entry: str) -> str:
        """Convert one history paragraph into a compact rolling-summary bullet."""
        return normalize_summary_entry(entry)

    @staticmethod
    def _is_background_summary_chunk(messages: list[dict[str, object]]) -> bool:
        """Detect heartbeat/postprocess chunks that should not pollute session summary."""
        return is_background_summary_chunk(messages)

    def _merge_rolling_summary(self, existing: str, new_entry: str) -> str:
        """Append one summary bullet and trim from the front to fit the budget."""
        return merge_rolling_summary(
            existing,
            new_entry,
            max_entries=self._MAX_SUMMARY_ENTRIES,
            budget_tokens=self.budget.summary_tokens,
        )

    def _update_session_summary(
        self,
        session: Session,
        entry: str | None,
        messages: list[dict[str, object]] | None = None,
    ) -> None:
        """Fold the latest archived history entry into session metadata."""
        if not entry:
            return
        if messages and self._is_background_summary_chunk(messages):
            return
        metadata = dict(session.metadata or {})
        current = str(metadata.get("rolling_summary") or "")
        merged = self._merge_rolling_summary(current, entry)
        if merged:
            metadata["rolling_summary"] = merged
            metadata["rolling_summary_updated_at"] = datetime.now().isoformat()
        else:
            metadata.pop("rolling_summary", None)
            metadata.pop("rolling_summary_updated_at", None)
        session.metadata = metadata

    def select_history_for_prompt(self, session: Session) -> list[dict[str, Any]]:
        """Return only the recent unconsolidated history slice for the main prompt."""
        return session.get_recent_history(max_tokens=self.budget.recent_history_tokens)

    async def consolidate_messages(
        self,
        messages: list[dict[str, object]],
        *,
        session: Session | None = None,
    ) -> bool:
        """Archive a selected message chunk into persistent memory."""
        ok = await self.store.consolidate(messages, self.provider, self.model)
        if ok and session is not None:
            self._update_session_summary(
                session,
                self.store.consume_last_archive_entry(),
                messages,
            )
        return ok

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = self.select_history_for_prompt(session)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
            session_metadata=session.metadata,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive_unconsolidated(self, session: Session) -> bool:
        """Archive the full unconsolidated tail for /new-style session rollover."""
        lock = self.get_lock(session.key)
        async with lock:
            snapshot = session.messages[session.last_consolidated:]
            if not snapshot:
                return True
            return await self.consolidate_messages(snapshot, session=session)

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """Keep prompt growth stable by consolidating before the full window is exhausted."""
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            budget = self.budget
            estimated, source = self.estimate_session_prompt_tokens(session)
            unconsolidated_tokens = session.get_unconsolidated_token_count()
            estimated = max(0, estimated)
            if (
                estimated < budget.soft_limit_tokens
                and unconsolidated_tokens <= budget.recent_history_tokens
            ):
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    budget.soft_limit_tokens,
                    source,
                )
                return

            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if (
                    estimated <= budget.target_tokens
                    and unconsolidated_tokens <= budget.recent_history_tokens
                ):
                    return

                tokens_to_remove = max(
                    1,
                    estimated - budget.target_tokens,
                    unconsolidated_tokens - budget.recent_history_tokens,
                )
                boundary = self.pick_consolidation_boundary(session, tokens_to_remove)
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    budget.soft_limit_tokens,
                    source,
                    len(chunk),
                )
                if not await self.consolidate_messages(chunk, session=session):
                    return
                session.last_consolidated = end_idx
                self.sessions.save(session)

                estimated, source = self.estimate_session_prompt_tokens(session)
                estimated = max(0, estimated)
                unconsolidated_tokens = session.get_unconsolidated_token_count()
