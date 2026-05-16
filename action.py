"""Action layer — the stdio MCP client + artifact-handle offload.

Manages the lifecycle of the `python mcp_server.py` subprocess and
turns every Decision's `ToolCall` into an `ActionResult`.

Two responsibilities:

1. **Stdio transport** — uses the MCP Python SDK's `stdio_client` to
   spawn the server once at startup and reuse the same JSON-RPC
   session across iterations. Retries once on tool error before
   surfacing the failure.

2. **Artifact-handle guard** — when a tool's payload is large
   enough that re-shipping it through the LLM on every iteration
   would burn the context budget, Action offloads the bytes to the
   `ArtifactStore` (in memory.py) and returns an `ActionResult`
   with `artifact_id` set + a short descriptor. The agent loop
   records this as a `tool_outcome` MemoryItem; later iterations
   can `attach` the artifact to a goal so Decision sees the bytes
   in its prompt without re-calling the tool.

The threshold (ARTIFACT_THRESHOLD_BYTES) is set at 4 KB — small
enough that a `fetch_url` response or a heavy `read_file` always
offloads, large enough that tool confirmations and small
`web_search` / `list_dir` / `get_time` results stay inline.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client

from schemas import ActionResult, ToolCall

if TYPE_CHECKING:
    from memory import ArtifactStore

log = logging.getLogger(__name__)

ARTIFACT_THRESHOLD_BYTES = 4 * 1024  # 4 KB
PREVIEW_CHARS = 240                  # length of the inline preview kept on offload


class ActionError(RuntimeError):
    """Raised when the MCP server returns isError=True for a tool call."""


class Action:
    """Long-lived stdio MCP session + per-call dispatch. Construct
    once, call `start()` before the loop, `stop()` after, `execute()`
    inside the loop."""

    def __init__(self, server_script: Path, artifacts: "ArtifactStore") -> None:
        self._server_script = server_script
        self._artifacts = artifacts
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._stack is not None:
            raise RuntimeError("Action.start() called twice")
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(self._server_script)],
            env=None,
        )
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            await self._session.initialize()
        except Exception:
            # On startup failure, undo whatever we did so the caller can retry.
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._stack is None:
            return
        try:
            await self._stack.aclose()
        finally:
            self._stack = None
            self._session = None

    async def __aenter__(self) -> "Action":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def execute(self, tool_call: ToolCall) -> ActionResult:
        """Run one tool, with one retry on transport / server error.
        Always returns an ActionResult — errors become status='error'
        rather than raised exceptions, so the loop can surface them
        to the model on the next turn."""
        if self._session is None:
            raise RuntimeError("Action.execute() before start()")
        t0 = time.perf_counter()
        try:
            return await self._call_once(tool_call, t0, retried=False)
        except Exception as first_err:
            log.info("action: %s failed once (%s) — retrying", tool_call.name, first_err)
            try:
                return await self._call_once(tool_call, t0, retried=True)
            except Exception as second_err:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                return ActionResult(
                    tool=tool_call.name,
                    arguments=tool_call.arguments,
                    status="error",
                    error=f"{second_err} (first attempt: {first_err})",
                    retried=True,
                    duration_ms=duration_ms,
                )

    async def _call_once(
        self, tool_call: ToolCall, t0: float, retried: bool
    ) -> ActionResult:
        assert self._session is not None
        result = await self._session.call_tool(
            name=tool_call.name, arguments=tool_call.arguments
        )
        duration_ms = int((time.perf_counter() - t0) * 1000)

        # MCP CallToolResult.content is a list of content blocks (TextContent
        # etc.). FastMCP wraps non-string return values into a single
        # TextContent whose .text is the JSON-encoded payload.
        text_payload = _first_text_block(result)
        if getattr(result, "isError", False):
            raise ActionError(text_payload or "tool returned isError")

        # Try to JSON-decode; fall back to the raw text if it isn't JSON.
        try:
            payload: Any = json.loads(text_payload) if text_payload else None
        except json.JSONDecodeError:
            payload = text_payload

        # Decide whether to offload based on the size of the JSON-encoded
        # payload. We use the wire size, not the in-memory size, because
        # the LLM sees the wire form.
        encoded = (
            text_payload
            if text_payload is not None
            else json.dumps(payload, default=str)
        )
        encoded_bytes = encoded.encode("utf-8")

        if len(encoded_bytes) > ARTIFACT_THRESHOLD_BYTES:
            descriptor = _summarize_for_artifact(tool_call, payload, len(encoded_bytes))
            artifact = self._artifacts.store(
                content=encoded_bytes,
                content_type=_guess_content_type(tool_call, payload),
                source=f"tool:{tool_call.name}",
                descriptor=descriptor,
            )
            preview = (
                encoded[:PREVIEW_CHARS] + "…"
                if len(encoded) > PREVIEW_CHARS
                else encoded
            )
            return ActionResult(
                tool=tool_call.name,
                arguments=tool_call.arguments,
                status="ok",
                artifact_id=artifact.id,
                result=preview,
                retried=retried,
                duration_ms=duration_ms,
            )

        # Small enough to inline.
        return ActionResult(
            tool=tool_call.name,
            arguments=tool_call.arguments,
            status="ok",
            result=payload,
            retried=retried,
            duration_ms=duration_ms,
        )


# ----------------------------------------------------------------------
# Helpers — kept module-level so they're trivial to unit-test.
# ----------------------------------------------------------------------


def _first_text_block(call_result: Any) -> str | None:
    """Pull the first TextContent.text out of a CallToolResult.content
    list. Returns None if there are no text blocks."""
    content = getattr(call_result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return None


def _guess_content_type(tool_call: ToolCall, payload: Any) -> str:
    """Pick a content type for the artifact metadata. Falls back to
    application/json — only fetch_url currently flags markdown."""
    if tool_call.name == "fetch_url":
        if isinstance(payload, dict) and payload.get("content_type"):
            return str(payload["content_type"])
        return "text/markdown"
    return "application/json"


def _summarize_for_artifact(
    tool_call: ToolCall, payload: Any, encoded_size: int
) -> str:
    """One-line descriptor recorded with the artifact + echoed into
    memory hits. Used by perception's force-attach heuristic to
    identify which artifact to attach to a synthesis goal."""
    name = tool_call.name
    args = tool_call.arguments or {}
    if name == "fetch_url":
        url = args.get("url", "?")
        return f"fetched {url} ({encoded_size} bytes)"
    if name == "web_search":
        q = args.get("query", "?")
        n = len(payload) if isinstance(payload, list) else "?"
        return f"web_search '{q}' → {n} results ({encoded_size} bytes)"
    if name == "read_file":
        path = args.get("path", "?")
        return f"read_file {path} ({encoded_size} bytes)"
    return f"{name}({', '.join(args.keys())}) → {encoded_size} bytes"
