"""Cron tools for scheduling reminders and tasks."""

from contextvars import ContextVar
from typing import Any, Literal

from nanobot.agent.tools.base import Tool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule

CRON_VISIBLE_TOOL_NAMES = frozenset({
    "cron_add_once",
    "cron_add_interval",
    "cron_add_cron",
    "cron_list",
    "cron_remove",
})
CRON_MUTATING_TOOL_NAMES = frozenset({
    "cron",
    "cron_add_once",
    "cron_add_interval",
    "cron_add_cron",
    "cron_remove",
})
_DELIVER_SCHEMA: dict[str, Any] = {
    "type": "boolean",
    "description": "Notify the user when it runs",
}
_EXECUTION_POLICY_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["isolated-per-job", "isolated-per-run", "reuse-origin-session"],
    "description": "Execution session policy",
}


class CronTool(Tool):
    """Internal cron backend used by narrower LLM-facing wrappers."""

    def __init__(
        self,
        cron_service: CronService,
        default_deliver: bool = False,
        default_execution_policy: Literal[
            "isolated-per-job",
            "isolated-per-run",
            "reuse-origin-session",
        ] = "isolated-per-job",
    ):
        self._cron = cron_service
        self._default_deliver = default_deliver
        self._default_execution_policy = default_execution_policy
        self._channel_ctx: ContextVar[str] = ContextVar("cron_channel", default="")
        self._chat_id_ctx: ContextVar[str] = ContextVar("cron_chat_id", default="")
        self._tenant_key_ctx: ContextVar[str] = ContextVar("cron_tenant_key", default="__default__")
        self._session_key_ctx: ContextVar[str] = ContextVar("cron_session_key", default="cli:direct")
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(
        self,
        channel: str,
        chat_id: str,
        session_key: str | None = None,
        tenant_key: str | None = None,
    ) -> None:
        """Set the current session context for delivery."""
        self._channel_ctx.set(channel)
        self._chat_id_ctx.set(chat_id)
        self._session_key_ctx.set(session_key or f"{channel}:{chat_id}")
        self._tenant_key_ctx.set(tenant_key or "__default__")

    def set_cron_context(self, active: bool):
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return "Internal cron backend."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove"],
                    "description": "add, list, or remove",
                },
                "message": {"type": "string", "description": "Reminder text"},
                "every_seconds": {
                    "type": "integer",
                    "description": "Repeat interval in seconds",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Cron expression",
                },
                "tz": {
                    "type": "string",
                    "description": "Timezone for cron_expr",
                },
                "at": {
                    "type": "string",
                    "description": "One-time ISO datetime",
                },
                "job_id": {"type": "string", "description": "Job ID"},
                "deliver": {
                    "type": "boolean",
                    "description": "Notify the user when it runs",
                },
                "execution_policy": {
                    "type": "string",
                    "enum": ["isolated-per-job", "isolated-per-run", "reuse-origin-session"],
                    "description": "Execution session policy",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        deliver: bool | None = None,
        execution_policy: Literal[
            "isolated-per-job",
            "isolated-per-run",
            "reuse-origin-session",
        ] | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            if self._in_cron_context.get():
                return "Error: cannot schedule new jobs from within a cron job execution"
            return self._add_job(message, every_seconds, cron_expr, tz, at, deliver, execution_policy)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def normalize_action_params(self, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """Validate and normalize one cron action into backend-ready params."""
        action = str(params.get("action") or "").strip().lower()
        if action == "add":
            normalized, error = self._normalize_add_params(
                message=str(params.get("message") or ""),
                every_seconds=params.get("every_seconds"),
                cron_expr=params.get("cron_expr"),
                tz=params.get("tz"),
                at=params.get("at"),
                deliver=params.get("deliver"),
                execution_policy=params.get("execution_policy"),
            )
            if error:
                return None, error
            result: dict[str, Any] = {
                "action": "add",
                "message": str(normalized["message"]),
                "deliver": bool(normalized["deliver"]),
                "execution_policy": str(normalized["execution_policy"]),
            }
            if normalized.get("every_seconds") is not None:
                result["every_seconds"] = normalized["every_seconds"]
            if normalized.get("cron_expr") is not None:
                result["cron_expr"] = normalized["cron_expr"]
            if normalized.get("tz") is not None:
                result["tz"] = normalized["tz"]
            if normalized.get("at") is not None:
                result["at"] = normalized["at"]
            return result, None
        if action == "list":
            return {"action": "list"}, None
        if action == "remove":
            job_id = str(params.get("job_id") or "").strip()
            if not job_id:
                return None, "Error: job_id is required for remove"
            return {"action": "remove", "job_id": job_id}, None
        return None, f"Error: unknown action '{action}'"

    def preflight(self, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """Validate and normalize params before deferring execution."""
        return self.normalize_action_params(dict(params))

    def _add_job(
        self,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
        deliver: bool | None,
        execution_policy: Literal[
            "isolated-per-job",
            "isolated-per-run",
            "reuse-origin-session",
        ] | None,
    ) -> str:
        normalized, error = self._normalize_add_params(
            message=message,
            every_seconds=every_seconds,
            cron_expr=cron_expr,
            tz=tz,
            at=at,
            deliver=deliver,
            execution_policy=execution_policy,
        )
        if error:
            return error
        schedule = normalized["schedule"]
        delete_after = bool(normalized["delete_after"])
        channel = str(normalized["channel"])
        chat_id = str(normalized["chat_id"])
        tenant_key = str(normalized["tenant_key"])
        session_key = str(normalized["session_key"])

        job = self._cron.add_job(
            name=message[:30],
            schedule=schedule,
            message=message,
            deliver=bool(normalized["deliver"]),
            channel=channel,
            to=chat_id,
            tenant_key=tenant_key,
            session_key=session_key,
            origin_session_key=session_key,
            execution_policy=str(normalized["execution_policy"]),
            delete_after_run=delete_after,
        )
        notify_text = "with user notification" if job.payload.deliver else "without user notification"
        return (
            f"Created job '{job.name}' (id: {job.id}, {notify_text}, "
            f"policy: {job.payload.execution_policy})"
        )

    def _normalize_add_params(
        self,
        *,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
        deliver: bool | None,
        execution_policy: Literal[
            "isolated-per-job",
            "isolated-per-run",
            "reuse-origin-session",
        ] | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not message:
            return None, "Error: message is required for add"
        channel = self._channel_ctx.get()
        chat_id = self._chat_id_ctx.get()
        tenant_key = self._tenant_key_ctx.get()
        session_key = self._session_key_ctx.get()
        if not channel or not chat_id:
            return None, "Error: no session context (channel/chat_id)"

        normalized_tz = tz
        if normalized_tz and at and not cron_expr:
            normalized_tz = None
        if normalized_tz and not cron_expr:
            return None, "Error: tz can only be used with cron_expr"
        if normalized_tz:
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(normalized_tz)
            except (KeyError, Exception):
                return None, f"Error: unknown timezone '{normalized_tz}'"

        schedule: CronSchedule
        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=normalized_tz)
        elif at:
            from datetime import datetime

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return (
                    None,
                    f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS",
                )
            schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
            delete_after = True
        else:
            return None, "Error: either every_seconds, cron_expr, or at is required"

        return (
            {
                "message": message,
                "every_seconds": every_seconds,
                "cron_expr": cron_expr,
                "tz": normalized_tz,
                "at": at,
                "deliver": self._default_deliver if deliver is None else bool(deliver),
                "execution_policy": execution_policy or self._default_execution_policy,
                "schedule": schedule,
                "delete_after": delete_after,
                "channel": channel,
                "chat_id": chat_id,
                "tenant_key": tenant_key,
                "session_key": session_key,
            },
            None,
        )

    def _list_jobs(self) -> str:
        tenant_key = self._tenant_key_ctx.get()
        jobs = [
            j for j in self._cron.list_jobs()
            if (j.payload.tenant_key or "__default__") == tenant_key
        ]
        if not jobs:
            return "No scheduled jobs."
        lines = [
            (
                f"- {j.name} (id: {j.id}, {j.schedule.kind}, "
                f"deliver: {j.payload.deliver}, "
                f"origin: {j.payload.origin_session_key or '-'}, "
                f"policy: {j.payload.execution_policy or 'isolated-per-job'})"
            )
            for j in jobs
        ]
        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        tenant_key = self._tenant_key_ctx.get()
        jobs = self._cron.list_jobs(include_disabled=True)
        job = next((j for j in jobs if j.id == job_id), None)
        if not job:
            return f"Job {job_id} not found"
        if (job.payload.tenant_key or "__default__") != tenant_key:
            return f"Job {job_id} not found"
        if self._cron.remove_job(job_id):
            return f"Removed job {job_id}"
        return f"Job {job_id} not found"


class _CronFacadeTool(Tool):
    """Narrow wrapper around the internal cron backend."""

    def __init__(self, cron_tool: CronTool):
        self._cron_tool = cron_tool

    def _build_backend_params(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def _to_wrapper_params(self, normalized: dict[str, Any]) -> dict[str, Any]:
        params = dict(normalized)
        params.pop("action", None)
        return params

    def preflight(self, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        normalized, error = self._cron_tool.normalize_action_params(
            self._build_backend_params(**params)
        )
        if error:
            return None, error
        return self._to_wrapper_params(normalized or {}), None

    async def execute(self, **kwargs: Any) -> str:
        return await self._cron_tool.execute(**self._build_backend_params(**kwargs))


class CronAddOnceTool(_CronFacadeTool):
    @property
    def name(self) -> str:
        return "cron_add_once"

    @property
    def description(self) -> str:
        return "Schedule a one-time reminder."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Reminder text"},
                "at": {"type": "string", "description": "One-time ISO datetime"},
                "deliver": dict(_DELIVER_SCHEMA),
                "execution_policy": dict(_EXECUTION_POLICY_SCHEMA),
            },
            "required": ["message", "at"],
        }

    def _build_backend_params(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "action": "add",
            "message": kwargs.get("message", ""),
            "at": kwargs.get("at"),
            "deliver": kwargs.get("deliver"),
            "execution_policy": kwargs.get("execution_policy"),
        }


class CronAddIntervalTool(_CronFacadeTool):
    @property
    def name(self) -> str:
        return "cron_add_interval"

    @property
    def description(self) -> str:
        return "Schedule a repeating reminder by interval."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Reminder text"},
                "every_seconds": {
                    "type": "integer",
                    "description": "Repeat interval in seconds",
                    "minimum": 1,
                },
                "deliver": dict(_DELIVER_SCHEMA),
                "execution_policy": dict(_EXECUTION_POLICY_SCHEMA),
            },
            "required": ["message", "every_seconds"],
        }

    def _build_backend_params(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "action": "add",
            "message": kwargs.get("message", ""),
            "every_seconds": kwargs.get("every_seconds"),
            "deliver": kwargs.get("deliver"),
            "execution_policy": kwargs.get("execution_policy"),
        }


class CronAddCronTool(_CronFacadeTool):
    @property
    def name(self) -> str:
        return "cron_add_cron"

    @property
    def description(self) -> str:
        return "Schedule a reminder with a cron expression."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Reminder text"},
                "cron_expr": {"type": "string", "description": "Cron expression"},
                "tz": {"type": "string", "description": "Timezone"},
                "deliver": dict(_DELIVER_SCHEMA),
                "execution_policy": dict(_EXECUTION_POLICY_SCHEMA),
            },
            "required": ["message", "cron_expr"],
        }

    def _build_backend_params(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "action": "add",
            "message": kwargs.get("message", ""),
            "cron_expr": kwargs.get("cron_expr"),
            "tz": kwargs.get("tz"),
            "deliver": kwargs.get("deliver"),
            "execution_policy": kwargs.get("execution_policy"),
        }


class CronListTool(_CronFacadeTool):
    @property
    def name(self) -> str:
        return "cron_list"

    @property
    def description(self) -> str:
        return "List scheduled reminders for the current tenant."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    def _build_backend_params(self, **kwargs: Any) -> dict[str, Any]:
        return {"action": "list"}


class CronRemoveTool(_CronFacadeTool):
    @property
    def name(self) -> str:
        return "cron_remove"

    @property
    def description(self) -> str:
        return "Remove one scheduled reminder."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job ID"},
            },
            "required": ["job_id"],
        }

    def _build_backend_params(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "action": "remove",
            "job_id": kwargs.get("job_id"),
        }
