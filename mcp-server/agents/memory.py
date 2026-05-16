"""Per-agent memory store.

Each agent in the multi-agent system (orchestrator + workspace +
research) owns one of these. It survives across /agents/run requests
within the server's lifetime — server restart clears it. This is
the long-term scratchpad node from the architecture diagram.

`history` is a bounded ring of {task → short summary} entries written
by SubAgent.run() at the end of every invocation. `facts` is a
free-form key/value scratchpad agents can write to during a run
(currently unused by the agents themselves, but available for callers).

serialize() returns a markdown block that SubAgent prepends to its
next user message so the model sees "what this agent already knows"
before planning its next action.
"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

MEMORY_HISTORY_CAP = 5
MEMORY_SUMMARY_CHARS = 500
MEMORY_FACT_CHARS = 200


class AgentMemory:
    """Bounded history + free-form facts. Thread-safe is NOT required —
    each agent runs sequentially within a single asyncio event loop."""

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self.history: deque[dict[str, Any]] = deque(maxlen=MEMORY_HISTORY_CAP)
        self.facts: dict[str, Any] = {}

    def record_call(self, task: str, summary: Any) -> None:
        s = summary if isinstance(summary, str) else json.dumps(summary, default=str)
        self.history.append({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "task": task,
            "summary": s if len(s) <= MEMORY_SUMMARY_CHARS else s[:MEMORY_SUMMARY_CHARS] + "…",
        })

    def set_fact(self, key: str, value: Any) -> None:
        self.facts[key] = value

    def get_fact(self, key: str) -> Any:
        return self.facts.get(key)

    def serialize(self) -> str:
        """Render the memory as a markdown block. Returns "" when empty
        so callers don't pollute prompts with an empty header."""
        if not self.history and not self.facts:
            return ""
        lines = [f"## Memory ({self.agent_name})"]
        if self.history:
            lines.append("### Prior tasks in this session")
            for h in self.history:
                lines.append(f"- **{h['task']}** → {h['summary']}")
        if self.facts:
            lines.append("### Known facts")
            for k, v in self.facts.items():
                val = v if isinstance(v, str) else json.dumps(v, default=str)
                if len(val) > MEMORY_FACT_CHARS:
                    val = val[:MEMORY_FACT_CHARS] + "…"
                lines.append(f"- {k}: {val}")
        return "\n".join(lines)

    def clear(self) -> None:
        self.history.clear()
        self.facts.clear()
