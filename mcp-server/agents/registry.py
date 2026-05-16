"""Agent registry — builds the orchestrator + sub-agents at server
startup and wires tool resolution back through FastMCP's registered
tools.

Lifecycle:
    1. server.py registers all @mcp.tool functions
    2. server.py calls `await prepare(mcp)` once during startup
    3. prepare() awaits mcp.list_tools(), partitions the result into
       the four agent-scoped lists, then constructs the workspace +
       research SubAgent instances at module scope (so their
       AgentMemory persists across requests).
    4. Each /agents/run request calls `await run(query, emit, tz)`
       which builds a fresh orchestrator (callbacks differ per
       request) and drives it to completion.

Tool execution: agents resolve tool calls via `mcp.call_tool(name,
args)` so the same Pydantic validation + per-tool implementation
that the MCP transport uses is reused unchanged. The per-request
user timezone is propagated via a contextvar so the executor can
auto-inject it into every tool call (matching the JS MCP client's
behavior).
"""

from __future__ import annotations

import contextvars
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .main.prompt import ORCHESTRATOR_SYSTEM_PROMPT
from .main.tools import DELEGATE_TOOL, MAIN_DIRECT_TOOL_NAMES
from .research.prompt import RESEARCH_SYSTEM_PROMPT
from .research.tools import RESEARCH_TOOL_NAMES
from .sub_agent import SubAgent, reset_step_counter
from .workspace.prompt import WORKSPACE_SYSTEM_PROMPT
from .workspace.tools import WORKSPACE_TOOL_NAMES

ORCHESTRATOR_MAX_ITERATIONS = 10  # sub-agents inherit SUB_AGENT_MAX_ITERATIONS (6)

# Set per-request at the top of run() so the in-process tool executor
# can inject userTimeZone into every tool call's args, exactly as the
# old mcp-client.js did on the wire.
_user_tz_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agents_user_tz", default=None
)

# Populated by prepare(). Empty until then.
_mcp: FastMCP | None = None
_main_tools: list[dict[str, Any]] = []
_workspace_tools: list[dict[str, Any]] = []
_research_tools: list[dict[str, Any]] = []
_workspace_agent: SubAgent | None = None
_research_agent: SubAgent | None = None


async def prepare(mcp: FastMCP) -> None:
    """Pull the registered tool schemas from FastMCP, partition them
    across the agents, and construct the persistent sub-agent
    instances. Called once at server startup."""
    global _mcp, _workspace_agent, _research_agent

    _mcp = mcp

    fresh = await mcp.list_tools()  # MCP `Tool` objects
    _main_tools.clear()
    _workspace_tools.clear()
    _research_tools.clear()

    # Orchestrator always sees `delegate` first in its tool list.
    _main_tools.append(DELEGATE_TOOL)

    for t in fresh:
        descriptor = {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema or {},
        }
        if t.name in WORKSPACE_TOOL_NAMES:
            _workspace_tools.append(descriptor)
        elif t.name in RESEARCH_TOOL_NAMES:
            _research_tools.append(descriptor)
        elif t.name in MAIN_DIRECT_TOOL_NAMES:
            _main_tools.append(descriptor)
        # Tools not assigned to an agent are silently dropped.

    _workspace_agent = SubAgent(
        name="workspace",
        system_prompt=WORKSPACE_SYSTEM_PROMPT,
        tools=_workspace_tools,
        tool_executor=_execute_mcp_tool,
    )
    _research_agent = SubAgent(
        name="research",
        system_prompt=RESEARCH_SYSTEM_PROMPT,
        tools=_research_tools,
        tool_executor=_execute_mcp_tool,
    )


async def _execute_mcp_tool(tool_name: str, tool_input: dict[str, Any]) -> Any:
    """Resolve a tool call by routing through the FastMCP-registered
    tool. Auto-injects userTimeZone from the contextvar so the
    matching @mcp.tool wrapper sees the user's local zone even though
    we're not going through the MCP wire protocol."""
    if _mcp is None:
        raise RuntimeError("agents.registry.prepare() was not called before use.")

    args = dict(tool_input or {})
    tz = _user_tz_var.get()
    if tz:
        args.setdefault("userTimeZone", tz)

    # FastMCP returns a list of content blocks. Our tools always emit
    # a single text block of JSON (see server.py `_wrap_text`), so we
    # unwrap that here for parity with the JS MCP client.
    content_blocks = await _mcp.call_tool(tool_name, args)
    return _unwrap_mcp_content(content_blocks)


def _unwrap_mcp_content(content: Any) -> Any:
    # FastMCP's call_tool can return either a list of Content blocks
    # or a (content, structured_dict) tuple depending on version. We
    # accept either and prefer the JSON-decoded text payload.
    blocks = content
    if isinstance(content, tuple) and content:
        blocks = content[0]
    if not blocks:
        return None
    first = blocks[0] if isinstance(blocks, (list, tuple)) else blocks
    text = getattr(first, "text", None)
    if isinstance(text, str):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return first


async def run(query: str, emit, *, user_time_zone: str | None = None) -> dict[str, Any]:
    """Public entry point — invoked by runner.py per /agents/run
    request. Resets the step counter, scopes the user timezone for
    the duration of the run, builds a fresh orchestrator (with
    per-request callbacks baked into the delegate handler) and drives
    it to completion."""
    if _workspace_agent is None or _research_agent is None:
        raise RuntimeError("agents.registry.prepare() was not called before run().")

    reset_step_counter()
    token = _user_tz_var.set(user_time_zone)
    try:
        sub_agents: dict[str, SubAgent] = {
            "workspace": _workspace_agent,
            "research": _research_agent,
        }

        async def delegate_handler(args: dict[str, Any]) -> dict[str, Any]:
            agent_name = args.get("agent")
            task = args.get("task", "")
            sub = sub_agents.get(agent_name)
            if sub is None:
                raise RuntimeError(f"Unknown sub-agent: {agent_name!r}")
            outcome = await sub.run(task, emit, user_time_zone=user_time_zone)
            return {
                "agent": agent_name,
                "stopReason": outcome["stop_reason"],
                "summary": outcome["text"] or f"({agent_name} produced no final text)",
            }

        orchestrator = SubAgent(
            name="main",
            system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
            tools=_main_tools,
            tool_executor=_execute_mcp_tool,
            tool_handlers={"delegate": delegate_handler},
            max_iterations=ORCHESTRATOR_MAX_ITERATIONS,
        )

        return await orchestrator.run(query, emit, user_time_zone=user_time_zone)
    finally:
        _user_tz_var.reset(token)
