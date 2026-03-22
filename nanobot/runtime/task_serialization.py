"""Serialization helpers for runtime task payloads."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from nanobot.agent.postprocess import DeferredToolAction
from nanobot.bus.events import OutboundMessage
from nanobot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule


def serialize_deferred_actions(actions: list[DeferredToolAction]) -> list[dict[str, Any]]:
    return [
        {
            "tool_name": action.tool_name,
            "params": action.params,
            "synthetic_result": action.synthetic_result,
        }
        for action in actions
    ]


def deserialize_deferred_actions(items: list[dict[str, Any]]) -> list[DeferredToolAction]:
    return [
        DeferredToolAction(
            tool_name=str(item["tool_name"]),
            params=dict(item.get("params") or {}),
            synthetic_result=str(item.get("synthetic_result") or ""),
        )
        for item in items
    ]


def serialize_cron_job(job: CronJob) -> dict[str, Any]:
    return asdict(job)


def deserialize_cron_job(payload: dict[str, Any]) -> CronJob:
    schedule = payload["schedule"]
    data = payload["payload"]
    state = payload["state"]
    return CronJob(
        id=str(payload["id"]),
        name=str(payload["name"]),
        enabled=bool(payload["enabled"]),
        schedule=CronSchedule(
            kind=schedule["kind"],
            at_ms=schedule.get("at_ms"),
            every_ms=schedule.get("every_ms"),
            expr=schedule.get("expr"),
            tz=schedule.get("tz"),
        ),
        payload=CronPayload(
            kind=data.get("kind", "agent_turn"),
            message=data.get("message", ""),
            deliver=data.get("deliver", False),
            channel=data.get("channel"),
            to=data.get("to"),
            tenant_key=data.get("tenant_key"),
            session_key=data.get("session_key"),
            origin_session_key=data.get("origin_session_key"),
            execution_policy=data.get("execution_policy"),
        ),
        state=CronJobState(
            next_run_at_ms=state.get("next_run_at_ms"),
            last_run_at_ms=state.get("last_run_at_ms"),
            last_status=state.get("last_status"),
            last_error=state.get("last_error"),
        ),
        created_at_ms=int(payload.get("created_at_ms") or 0),
        updated_at_ms=int(payload.get("updated_at_ms") or 0),
        delete_after_run=bool(payload.get("delete_after_run")),
    )


def serialize_outbound_message(message: OutboundMessage) -> dict[str, Any]:
    return {
        "channel": message.channel,
        "chat_id": message.chat_id,
        "content": message.content,
        "tenant_key": message.tenant_key,
        "session_key": message.session_key,
        "reply_to": message.reply_to,
        "media": list(message.media),
        "metadata": dict(message.metadata or {}),
    }


def deserialize_outbound_message(payload: dict[str, Any]) -> OutboundMessage:
    return OutboundMessage(
        channel=str(payload["channel"]),
        chat_id=str(payload["chat_id"]),
        content=str(payload.get("content") or ""),
        tenant_key=payload.get("tenant_key"),
        session_key=payload.get("session_key"),
        reply_to=payload.get("reply_to"),
        media=list(payload.get("media") or []),
        metadata=dict(payload.get("metadata") or {}),
    )
