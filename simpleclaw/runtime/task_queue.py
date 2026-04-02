"""Redis-backed background task queue with a local in-memory fallback."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol

from loguru import logger

from simpleclaw.runtime.task_protocol import TaskEnvelope, TaskStream


@dataclass(slots=True)
class TaskMessage:
    """One claimed queue message."""

    stream: TaskStream
    queue_id: str
    task: TaskEnvelope


class TaskQueue(Protocol):
    """Abstract queue used by chat-api, scheduler, and background workers."""

    async def enqueue(self, task: TaskEnvelope) -> str: ...
    async def consume(
        self,
        stream: TaskStream,
        *,
        consumer_group: str,
        consumer_name: str,
        count: int = 1,
    ) -> list[TaskMessage]: ...
    async def ack(self, message: TaskMessage, *, consumer_group: str) -> None: ...


class InMemoryTaskQueue:
    """Fallback queue for local single-process development."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[TaskMessage]] = defaultdict(asyncio.Queue)
        self._counter = 0

    async def enqueue(self, task: TaskEnvelope) -> str:
        self._counter += 1
        queue_id = f"{self._counter}-0"
        await self._queues[task.stream].put(
            TaskMessage(stream=task.stream, queue_id=queue_id, task=task)
        )
        return queue_id

    async def consume(
        self,
        stream: TaskStream,
        *,
        consumer_group: str,
        consumer_name: str,
        count: int = 1,
    ) -> list[TaskMessage]:
        del consumer_group, consumer_name
        items: list[TaskMessage] = []
        queue = self._queues[stream]
        first = await queue.get()
        items.append(first)
        while len(items) < count and not queue.empty():
            items.append(queue.get_nowait())
        return items

    async def ack(self, message: TaskMessage, *, consumer_group: str) -> None:
        del message, consumer_group


class RedisTaskQueue:
    """Redis Streams-backed queue."""

    def __init__(
        self,
        *,
        url: str,
        stream_prefix: str,
        stream_names: dict[TaskStream, str] | None = None,
        block_ms: int = 5000,
        batch_size: int = 8,
    ) -> None:
        self._url = url
        self._stream_prefix = stream_prefix.rstrip(":")
        self._stream_names = stream_names or {
            "postprocess": "postprocess",
            "background": "background",
            "outbound": "outbound",
            "dead-letter": "dead-letter",
        }
        self._block_ms = max(100, block_ms)
        self._batch_size = max(1, batch_size)
        self._client = None

    async def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from redis import asyncio as redis_asyncio
        except ImportError as exc:  # pragma: no cover - dependency/runtime guard
            raise RuntimeError("Redis support requires the 'redis' package") from exc
        self._client = redis_asyncio.from_url(self._url, decode_responses=True)
        return self._client

    def _stream_name(self, stream: TaskStream) -> str:
        return f"{self._stream_prefix}:{self._stream_names[stream]}"

    async def _ensure_group(self, stream: TaskStream, consumer_group: str) -> None:
        client = await self._get_client()
        try:
            await client.xgroup_create(
                name=self._stream_name(stream),
                groupname=consumer_group,
                id="0",
                mkstream=True,
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, task: TaskEnvelope) -> str:
        client = await self._get_client()
        return await client.xadd(
            self._stream_name(task.stream),
            {"payload": task.to_json()},
        )

    async def consume(
        self,
        stream: TaskStream,
        *,
        consumer_group: str,
        consumer_name: str,
        count: int = 1,
    ) -> list[TaskMessage]:
        await self._ensure_group(stream, consumer_group)
        client = await self._get_client()
        raw = await client.xreadgroup(
            groupname=consumer_group,
            consumername=consumer_name,
            streams={self._stream_name(stream): ">"},
            count=max(1, min(count, self._batch_size)),
            block=self._block_ms,
        )
        items: list[TaskMessage] = []
        for stream_name, entries in raw:
            del stream_name
            for queue_id, fields in entries:
                payload = fields.get("payload")
                if not payload:
                    logger.warning("Skipping empty task payload from stream {}", stream)
                    continue
                items.append(
                    TaskMessage(
                        stream=stream,
                        queue_id=queue_id,
                        task=TaskEnvelope.from_json(payload),
                    )
                )
        return items

    async def ack(self, message: TaskMessage, *, consumer_group: str) -> None:
        client = await self._get_client()
        await client.xack(self._stream_name(message.stream), consumer_group, message.queue_id)

