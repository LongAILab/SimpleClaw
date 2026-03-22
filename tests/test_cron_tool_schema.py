import pytest

from nanobot.agent.tools.cron import (
    CronAddIntervalTool,
    CronAddOnceTool,
    CronListTool,
    CronRemoveTool,
    CronTool,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.cron.service import CronService


@pytest.fixture()
def cron_backend(tmp_path):
    service = CronService(tmp_path / "cron" / "jobs.json")
    tool = CronTool(service)
    tool.set_context(
        channel="api",
        chat_id="main:tenant-a",
        session_key="main:tenant-a",
        tenant_key="tenant-a",
    )
    return service, tool


def _register_cron_wrappers(registry: ToolRegistry, backend: CronTool) -> None:
    registry.register(backend, expose_to_llm=False)
    registry.register(CronAddOnceTool(backend))
    registry.register(CronAddIntervalTool(backend))
    registry.register(CronListTool(backend))
    registry.register(CronRemoveTool(backend))


def test_tool_registry_hides_internal_cron_schema(cron_backend) -> None:
    _, backend = cron_backend
    registry = ToolRegistry()
    _register_cron_wrappers(registry, backend)

    names = {item["function"]["name"] for item in registry.get_definitions()}

    assert "cron" not in names
    assert names == {
        "cron_add_once",
        "cron_add_interval",
        "cron_list",
        "cron_remove",
    }


@pytest.mark.asyncio
async def test_wrappers_execute_full_cron_lifecycle(cron_backend) -> None:
    service, backend = cron_backend
    registry = ToolRegistry()
    _register_cron_wrappers(registry, backend)

    add_result = await registry.execute(
        "cron_add_interval",
        {
            "message": "喝水提醒",
            "every_seconds": "120",
        },
    )
    assert "Created job" in add_result
    jobs = service.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].schedule.kind == "every"

    list_result = await registry.execute("cron_list", {})
    assert "Scheduled jobs:" in list_result
    assert "喝水提醒" in list_result

    remove_result = await registry.execute("cron_remove", {"job_id": jobs[0].id})
    assert remove_result == f"Removed job {jobs[0].id}"
    assert service.list_jobs() == []
