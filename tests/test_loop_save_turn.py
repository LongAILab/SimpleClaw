from simpleclaw.agent.context import ContextBuilder
from simpleclaw.agent.loop import AgentLoop
from simpleclaw.agent.turn_commit import save_turn_messages
from simpleclaw.session.manager import Session

_TOOL_RESULT_MAX_CHARS = AgentLoop._TOOL_RESULT_MAX_CHARS


def _save_turn(session: Session, messages: list[dict], skip: int) -> None:
    save_turn_messages(session, messages, skip=skip, tool_result_max_chars=_TOOL_RESULT_MAX_CHARS)


def test_save_turn_skips_multimodal_user_when_only_runtime_context() -> None:
    session = Session(key="test:runtime-only")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    _save_turn(
        session,
        [{"role": "user", "content": [{"type": "text", "text": runtime}]}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_image_placeholder_after_runtime_strip() -> None:
    session = Session(key="test:image")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    _save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": runtime},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image]"}]


def test_save_turn_keeps_tool_results_under_16k() -> None:
    session = Session(key="test:tool-result")
    content = "x" * 12_000

    _save_turn(
        session,
        [{"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": content}],
        skip=0,
    )

    assert session.messages[0]["content"] == content
