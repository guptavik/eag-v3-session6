"""Generic LLM-driven agent. Drives the same run loop for the
orchestrator and both specialist sub-agents — only the system prompt,
tool list, and (for the orchestrator) the in-process `delegate` handler
differ between instances.

Each instance owns:
    name             identifier used for routing + UI labelling
    system_prompt    scoped instructions for this agent
    tools            list of tool descriptors (subset of the MCP tool list)
    memory           AgentMemory persisting across runs (server-lifetime)
    tool_handlers    optional in-process handlers, keyed by tool name —
                     the orchestrator uses this to route `delegate` to
                     the named sub-agent without going through the MCP
                     tool-call path.
    tool_executor    callable resolving a tool name + input to a JSON-able
                     result (typically wraps the MCP tools.py functions).
    max_iterations   per-agent loop cap (orchestrator: 10, sub-agents: 6)

.run(task, emitter, opts) drives a short tool-use loop:
    1. prepend the agent's memory to the user task
    2. call_llm with this agent's system prompt + tool subset
    3. for each tool_use block: resolve via tool_handlers, else tool_executor
    4. push tool_results, repeat
    5. return the final text + record a brief summary into memory

`emitter` is an async callable invoked at every step transition with
a dict describing the event. agents.runner enqueues these onto an
asyncio.Queue which it drains and writes out as SSE frames.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from .llm import call_llm, make_tool_results_message, make_user_text_message
from .memory import AgentMemory

SUB_AGENT_MAX_ITERATIONS = 6

Emitter = Callable[[dict[str, Any]], Awaitable[None]]
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[Any]]


class StepCounter:
    """Module-scope counter shared across all agents in one run, so
    orchestrator + sub-agent steps interleave in a single ordered
    chain on the UI. Reset by runner.run_agent() at the start of each
    request."""

    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n

    def reset(self) -> None:
        self.n = 0


_step_counter = StepCounter()


def reset_step_counter() -> None:
    _step_counter.reset()


def next_step_id() -> int:
    return _step_counter.next()


async def _execute_with_retry(
    tool_name: str,
    tool_input: dict[str, Any],
    executor: ToolExecutor,
    on_retry: Callable[[str], Awaitable[None]] | None,
) -> dict[str, Any]:
    """Try once; on failure invoke on_retry then try once more. Returns:
        {"ok": True,  "result": <...>, "retried": bool}
        {"ok": False, "error": str, "first_error": str, "retried": True}
    """
    try:
        return {"ok": True, "result": await executor(tool_name, tool_input), "retried": False}
    except Exception as err1:  # noqa: BLE001 — surface any failure to the model
        msg1 = str(err1) or err1.__class__.__name__
        if on_retry:
            await on_retry(msg1)
        try:
            return {
                "ok": True,
                "result": await executor(tool_name, tool_input),
                "retried": True,
            }
        except Exception as err2:  # noqa: BLE001
            msg2 = str(err2) or err2.__class__.__name__
            return {"ok": False, "error": msg2, "first_error": msg1, "retried": True}


class SubAgent:
    """Generic LLM-driven agent. Constructed once per role
    (orchestrator / workspace / research) and reused across runs.

    Memory is a singleton scoped to the agent instance — it survives
    across `run_agent()` calls within the server's lifetime."""

    def __init__(
        self,
        *,
        name: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
        tool_executor: ToolExecutor,
        tool_handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] | None = None,
        max_iterations: int | None = None,
    ) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools                                     # live reference
        self.tool_executor = tool_executor
        self.tool_handlers = tool_handlers or {}
        self.max_iterations = max_iterations or SUB_AGENT_MAX_ITERATIONS
        self.memory = AgentMemory(name)

    async def _execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        if tool_name in self.tool_handlers:
            return await self.tool_handlers[tool_name](tool_input or {})
        return await self.tool_executor(tool_name, tool_input or {})

    async def run(
        self,
        task: str,
        emit: Emitter,
        *,
        user_time_zone: str | None = None,
    ) -> dict[str, Any]:
        """Run one task to completion (or iteration cap). Returns
        {"text": str, "stop_reason": str}."""

        memory_block = self.memory.serialize()
        task_with_memory = (
            f"{memory_block}\n\n## Current task\n{task}" if memory_block else task
        )

        history: list[dict[str, Any]] = [make_user_text_message(task_with_memory)]
        final_text = ""
        stop_reason: str | None = None

        for _ in range(self.max_iterations):
            response = await call_llm(
                history,
                self.tools,
                system_prompt=self.system_prompt,
                user_time_zone=user_time_zone,
            )

            # Surface any text blocks before deciding what to do next so the
            # UI shows the model's planning prose as soon as it lands.
            text_blocks = [b for b in response["content"] if b["type"] == "text"]
            for tb in text_blocks:
                if tb["text"].strip():
                    await emit({
                        "kind": "assistant_text",
                        "agent": self.name,
                        "text": tb["text"],
                    })

            # Mirror the assistant turn into history. Gemini wants the
            # functionCall parts back verbatim on the next turn alongside
            # their matching functionResponse parts.
            history.append({
                "role": "model",
                "parts": _content_blocks_to_gemini_parts(response["content"]),
            })

            if response["stop_reason"] != "tool_use":
                final_text = "\n".join(t["text"] for t in text_blocks).strip()
                stop_reason = response["stop_reason"]
                break

            tool_use_blocks = [b for b in response["content"] if b["type"] == "tool_use"]
            tool_results: list[dict[str, Any]] = []

            for block in tool_use_blocks:
                step_id = next_step_id()
                await emit({
                    "kind": "step",
                    "stepId": step_id,
                    "agent": self.name,
                    "status": "loading",
                    "toolName": block["name"],
                    "toolInput": block["input"],
                })

                async def _on_retry(first_err_msg: str, _sid: int = step_id, _block: dict[str, Any] = block) -> None:
                    await emit({
                        "kind": "step",
                        "stepId": _sid,
                        "agent": self.name,
                        "status": "retrying",
                        "toolName": _block["name"],
                        "toolInput": _block["input"],
                        "error": first_err_msg,
                    })

                outcome = await _execute_with_retry(
                    block["name"], block["input"], self._execute_tool, _on_retry
                )

                if outcome["ok"]:
                    await emit({
                        "kind": "step",
                        "stepId": step_id,
                        "agent": self.name,
                        "status": "success",
                        "toolName": block["name"],
                        "toolInput": block["input"],
                        "result": outcome["result"],
                        "retried": outcome["retried"],
                    })
                    tool_results.append({
                        "name": block["name"],
                        "response": _ensure_json_object(outcome["result"]),
                    })
                else:
                    await emit({
                        "kind": "step",
                        "stepId": step_id,
                        "agent": self.name,
                        "status": "error",
                        "toolName": block["name"],
                        "toolInput": block["input"],
                        "error": outcome["error"],
                        "firstError": outcome.get("first_error"),
                        "retried": True,
                    })
                    tool_results.append({
                        "name": block["name"],
                        "is_error": True,
                        "error_message": f"Tool error after one retry: {outcome['error']}",
                    })

            history.append(make_tool_results_message(tool_results))

        if stop_reason is None:
            stop_reason = "max_iterations"

        self.memory.record_call(task, final_text)
        return {"text": final_text, "stop_reason": stop_reason}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _content_blocks_to_gemini_parts(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert our internal {type: "text"|"tool_use"} content list back
    to Gemini's parts shape so we can append the assistant turn to the
    history. Required because every functionCall part must be paired
    with a functionResponse part on the next turn for Gemini's tool-use
    contract."""
    parts: list[dict[str, Any]] = []
    for b in blocks:
        if b["type"] == "text":
            if b["text"]:
                parts.append({"text": b["text"]})
        elif b["type"] == "tool_use":
            parts.append({"functionCall": {"name": b["name"], "args": b["input"] or {}}})
    if not parts:
        # Gemini rejects empty parts arrays.
        parts.append({"text": " "})
    return parts


def _ensure_json_object(value: Any) -> dict[str, Any]:
    """Gemini's functionResponse.response must be a JSON object. Wrap
    primitives / lists / strings under a "result" key. Strings that
    happen to be JSON-encoded objects are parsed first (tools.py
    JSON-encodes everything as a text wire format)."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            value = parsed
        except json.JSONDecodeError:
            return {"result": value}
    if isinstance(value, dict):
        return value
    return {"result": value}
