from nanobot.api.server import _build_turn_timing_payload, _render_prompt_snapshot


def test_render_prompt_snapshot_includes_system_prompt_and_tools() -> None:
    snapshot = _render_prompt_snapshot(
        [
            {"role": "system", "content": "SYS PROMPT"},
            {"role": "user", "content": "你好"},
        ],
        [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "read",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        {
            "history": {"selected_messages": 2, "compacted": True},
        },
    )

    assert "## Full System Prompt" in snapshot
    assert "SYS PROMPT" in snapshot
    assert "## Tool Schemas" in snapshot
    assert "read_file" in snapshot
    assert "## Prompt Observability" in snapshot
    assert "selected_messages" in snapshot
    assert "## Request Messages" in snapshot
    assert "(system message shown above)" in snapshot
    assert "## Message 1 [SYSTEM]" not in snapshot
    assert "## Message 2 [USER]" in snapshot


def test_build_turn_timing_payload_computes_expected_breakdown() -> None:
    payload = _build_turn_timing_payload(
        request_started_at=8.0,
        prompt_snapshot_sent_at=8.125,
        llm_request_started_at=8.25,
        first_text_delta_at=10.0,
        final_response_at=10.5,
    )

    assert payload["accepted_to_prompt_snapshot_ms"] == 125
    assert payload["accepted_to_first_token_ms"] == 2000
    assert payload["accepted_to_final_ms"] == 2500
    assert payload["prompt_snapshot_to_first_token_ms"] == 1875
    assert payload["llm_wait_ms"] == 1750
    assert payload["streaming_after_first_token_ms"] == 500
