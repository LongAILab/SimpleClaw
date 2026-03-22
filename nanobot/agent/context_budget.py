"""Shared prompt-budget helpers."""

from __future__ import annotations

from dataclasses import dataclass

from nanobot.utils.helpers import estimate_message_tokens

_MAX_SOFT_LIMIT_TOKENS = 6000
_MAX_TARGET_TOKENS = 4500
_MAX_RECENT_HISTORY_TOKENS = 1536


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for free-form text."""
    return estimate_message_tokens({"role": "system", "content": text})


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Derived prompt-budget split based on the single public context window."""

    total_tokens: int
    soft_limit_tokens: int
    target_tokens: int
    recent_history_tokens: int
    summary_tokens: int
    memory_tokens: int


def derive_context_budget_tokens(total_tokens: int) -> ContextBudget:
    """Derive stable internal budgets from one public context window."""
    total = max(256, int(total_tokens or 0))
    soft_limit = min(_MAX_SOFT_LIMIT_TOKENS, max(64, int(total * 0.72)))
    target = min(_MAX_TARGET_TOKENS, max(48, int(total * 0.55)))
    recent_history = min(_MAX_RECENT_HISTORY_TOKENS, max(64, int(total * 0.12)))
    summary = min(1536, max(48, int(total * 0.04)))
    memory = min(2048, max(96, int(total * 0.06)))
    return ContextBudget(
        total_tokens=total,
        soft_limit_tokens=soft_limit,
        target_tokens=target,
        recent_history_tokens=recent_history,
        summary_tokens=summary,
        memory_tokens=memory,
    )
