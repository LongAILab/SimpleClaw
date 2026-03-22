"""Shared cron job execution helpers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Awaitable, Callable

from nanobot.bus.events import OutboundMessage
from nanobot.cron.types import CronJob

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop


def build_cron_execution_session_key(job: CronJob) -> str:
    """Return the isolated execution session key for one cron job.

    We intentionally do not reuse the origin session here, so cron execution
    never pollutes the user's main conversation context.
    """
    policy = job.payload.execution_policy or "isolated-per-job"
    if policy == "reuse-origin-session":
        return job.payload.origin_session_key or f"cron:{job.id}"
    if policy == "isolated-per-run":
        return f"cron:{job.id}:{int(time.time() * 1000)}"
    return f"cron:{job.id}"


def build_cron_reminder_note(job: CronJob) -> str:
    """Build the prompt injected into the agent when a cron job fires."""
    return (
        "[Scheduled Task] Timer finished.\n\n"
        f"Task '{job.name}' has been triggered.\n"
        f"Scheduled instruction: {job.payload.message}"
    )


async def execute_cron_job(
    *,
    agent_loop: AgentLoop,
    job: CronJob,
    default_channel: str,
    publish_outbound: Callable[[OutboundMessage], Awaitable[None]] | None = None,
) -> str | None:
    """Execute a cron job in an isolated cron session."""
    from nanobot.agent.tools.cron import CronTool
    from nanobot.agent.tools.message import MessageTool

    reminder_note = build_cron_reminder_note(job)

    cron_tool = agent_loop.tools.get("cron")
    cron_token = None
    if isinstance(cron_tool, CronTool):
        cron_token = cron_tool.set_cron_context(True)

    try:
        response = await agent_loop.process_direct(
            reminder_note,
            session_key=build_cron_execution_session_key(job),
            channel=job.payload.channel or default_channel,
            chat_id=job.payload.to or "direct",
            tenant_key=job.payload.tenant_key,
            outbound_session_key=job.payload.origin_session_key or job.payload.session_key,
            session_type="cron",
            origin_session_key=job.payload.origin_session_key or job.payload.session_key,
            lane="cron",
        )
    finally:
        if isinstance(cron_tool, CronTool) and cron_token is not None:
            cron_tool.reset_cron_context(cron_token)

    message_tool = agent_loop.tools.get("message")
    if isinstance(message_tool, MessageTool) and message_tool.sent_in_turn():
        return response

    if job.payload.deliver and job.payload.to and response and publish_outbound is not None:
        await publish_outbound(OutboundMessage(
            channel=job.payload.channel or default_channel,
            chat_id=job.payload.to,
            content=response,
            tenant_key=job.payload.tenant_key,
            session_key=job.payload.origin_session_key or job.payload.session_key,
            metadata={
                "_event": "cron_message",
                "_source": "cron",
                "_cron_job_id": job.id,
                "_cron_job_name": job.name,
            },
        ))

    return response
