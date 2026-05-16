"""SSE bridge between the in-process agent loop and the HTTP layer.

Exposes one public async generator, `stream_agent_run()`, that yields
text/event-stream-encoded events as the agents progress through their
loop. server.py mounts a /agents/run route that wraps the generator
in a StreamingResponse.

Event types (mirror the keys the old JS callbacks fired):
    step           tool-call lifecycle (loading → retrying? → success/error)
    assistant_text reasoning prose between tool calls (tagged with the agent)
    final_text     the orchestrator's last assistant message (the brief)
    done           run completed normally; payload includes stop_reason
    error          run aborted; payload includes a message

Each event is emitted in standard SSE framing:
    event: <kind>
    data: <json payload>
    \n

The event names are stable so the extension's SSE reader can dispatch
on them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from . import registry

log = logging.getLogger(__name__)

# Sentinel pushed into the queue when the agent run finishes (success
# or error). The drain loop uses it to exit cleanly without polling
# the underlying task.
_DONE = object()


async def stream_agent_run(
    query: str, *, user_time_zone: str | None = None
) -> AsyncIterator[bytes]:
    """Run the orchestrator and yield SSE-encoded events.

    Design:
      - An asyncio.Queue collects events emitted by SubAgent.run() at
        each step transition.
      - The agent run is launched as a Task so the generator can
        interleave drain + emit. If the client disconnects mid-stream
        and the consumer stops iterating, the Task is cancelled and
        any in-flight Gemini call is aborted (httpx supports cancel).
      - We avoid backpressure by using an unbounded queue. Volume is
        low (low-tens of events per run) so this is fine.
    """
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict[str, Any]) -> None:
        await queue.put(event)

    async def run_task() -> dict[str, Any] | None:
        try:
            return await registry.run(query, emit, user_time_zone=user_time_zone)
        finally:
            await queue.put(_DONE)

    task = asyncio.create_task(run_task())

    try:
        while True:
            event = await queue.get()
            if event is _DONE:
                break
            yield _format_sse(event["kind"], event)

        # Drain the task result and emit the final brief / done event.
        try:
            outcome = await task
        except Exception as exc:  # noqa: BLE001
            log.exception("agent run failed")
            yield _format_sse("error", {"message": str(exc) or exc.__class__.__name__})
            return

        if outcome is None:
            yield _format_sse("error", {"message": "Agent run produced no result."})
            return

        if outcome["stop_reason"] == "max_iterations":
            yield _format_sse(
                "error",
                {
                    "message": (
                        f"Stopped after {registry.ORCHESTRATOR_MAX_ITERATIONS} "
                        "iterations without a final answer. The agent may be stuck "
                        "in a loop."
                    ),
                    "stop_reason": "max_iterations",
                },
            )
            return

        if outcome["text"]:
            yield _format_sse("final_text", {"text": outcome["text"]})

        yield _format_sse("done", {"stop_reason": outcome["stop_reason"]})
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def _format_sse(event_name: str, payload: dict[str, Any]) -> bytes:
    """SSE framing: a `event:` line, one or more `data:` lines, blank
    line terminator. We keep payloads small (single-line JSON), so a
    single data: line is enough."""
    body = json.dumps(payload, default=str)
    return f"event: {event_name}\ndata: {body}\n\n".encode("utf-8")
