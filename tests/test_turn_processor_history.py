from simpleclaw.agent.turn_processor import TurnProcessor


class DummySession:
    def __init__(self) -> None:
        self.calls = 0

    def get_history(self, max_messages: int = 0):
        self.calls += 1
        return [{"role": "assistant", "content": f"history:{max_messages}"}]


def test_get_history_for_turn_supports_suppression() -> None:
    session = DummySession()

    assert TurnProcessor._get_history_for_turn(session, {"_suppress_history": True}) == []
    assert session.calls == 0

    history = TurnProcessor._get_history_for_turn(session, {})
    assert history == [{"role": "assistant", "content": "history:0"}]
    assert session.calls == 1


class DummyTool:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def set_context(self, channel: str, chat_id: str, *, session_key: str, tenant_key: str) -> None:
        self.calls.append(
            {
                "channel": channel,
                "chat_id": chat_id,
                "session_key": session_key,
                "tenant_key": tenant_key,
            }
        )


class DummyRegistry:
    def __init__(self, tools: dict[str, object]) -> None:
        self.tools = tools

    def get(self, name: str) -> object | None:
        return self.tools.get(name)


class DummyRuntime:
    workspace = "."
    subagents = None


def test_set_tool_context_prefers_origin_session_for_cron() -> None:
    cron_tool = DummyTool()
    processor = object.__new__(TurnProcessor)
    processor._tools = DummyRegistry({"cron": cron_tool})
    processor._restrict_to_workspace = True

    processor.set_tool_context(
        DummyRuntime(),
        "api",
        "main:tenant-b",
        "postprocess:main:tenant-b",
        "tenant-b",
        metadata={"_origin_session_key": "main:tenant-b"},
    )

    assert cron_tool.calls == [
        {
            "channel": "api",
            "chat_id": "main:tenant-b",
            "session_key": "main:tenant-b",
            "tenant_key": "tenant-b",
        }
    ]
