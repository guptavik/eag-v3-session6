"""Pydantic v2 contracts for the 4-layer cognitive agent.

The six **inter-layer** contracts (MemoryItem, Artifact, Goal, Observation,
ToolCall, DecisionOutput) are the canonical wire format between perception,
memory, decision, and action. They were specified upstream and must not
drift — no free-form dict passing between roles, no regex on LLM output.

The three **supporting** shapes (ActionResult, RunRecord, TraceEvent) are
for internal bookkeeping that doesn't cross an LLM boundary, kept here so
every module has one place to import contracts from.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# ID prefixes — every persistent record carries a typed prefix so the kind
# is visible at a glance in traces and on disk.
# ---------------------------------------------------------------------------

ARTIFACT_ID_PREFIX = "art:"
MEMORY_ID_PREFIX = "mem:"
GOAL_ID_PREFIX = "goal:"
RUN_ID_PREFIX = "run:"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_memory_id() -> str:
    return f"{MEMORY_ID_PREFIX}{uuid4().hex[:12]}"


def new_goal_id() -> str:
    return f"{GOAL_ID_PREFIX}{uuid4().hex[:8]}"


def new_run_id() -> str:
    # Sortable: timestamp prefix + short random suffix.
    return f"{RUN_ID_PREFIX}{_now().strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:6]}"


# ---------------------------------------------------------------------------
# Inter-layer contracts (provided by the assignment)
# ---------------------------------------------------------------------------


class MemoryItem(BaseModel):
    """One unit of durable memory.

    `kind` is the retention-policy lever:
        fact         — durable, indexed; survives across runs (e.g. "mom's birthday is …")
        preference   — durable, indexed; survives across runs (e.g. "my favorite city is …")
        tool_outcome — write-through cache for expensive tool calls (durable, freshness-bounded)
        scratchpad   — per-run working memory; can be GC'd at run end

    `keywords` are used for the keyword-based recall in memory.py.
    `descriptor` is the one-line human-readable summary (shown in traces).
    `value` is the structured payload (used by decision.py).
    `artifact_id` points into the artifact store for large blobs.
    """

    id: str
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str
    value: dict[str, Any]
    artifact_id: str | None = None
    source: str
    run_id: str
    goal_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: datetime

    model_config = ConfigDict(extra="forbid")


class Artifact(BaseModel):
    """Metadata record for a content-addressed blob in state/artifacts/.

    The bytes themselves live at state/artifacts/<sha256-prefix>.bin (or
    .md / .txt / .html when the content-type suggests a human-readable
    extension). The id format is "art:<first 16 hex chars of sha256>".
    """

    id: str
    content_type: str
    size_bytes: int
    source: str
    descriptor: str

    model_config = ConfigDict(extra="forbid")


class Goal(BaseModel):
    """One unit of work, emitted by perception, processed by the loop.

    `text` is a short imperative description ("Fetch the Wikipedia page
    for Claude Shannon"). Perception sets `attach_artifact_id` when the
    goal needs the bytes of an artifact loaded from memory — the loop
    reads it and prepends the content to Decision's prompt for this goal.
    """

    id: str
    text: str
    done: bool = False
    attach_artifact_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class Observation(BaseModel):
    """Perception output — the decomposition of the user query into goals.

    `goals` is processed in order. Once `goal.done = True`, it stays
    done (sticky-done invariant). The user-facing final answer is the
    DecisionOutput.answer for the LAST goal in the list.
    """

    goals: list[Goal]

    model_config = ConfigDict(extra="forbid")


class ToolCall(BaseModel):
    """The decision layer's choice when more data is needed.

    `name` must match one of the 9 MCP tool names declared by mcp_server.py.
    `arguments` is JSON-able; validation against the tool's input schema
    happens server-side via FastMCP/Pydantic.
    """

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class DecisionOutput(BaseModel):
    """Exactly one of {answer, tool_call} is populated (XOR).

    The model_validator enforces this so the loop can dispatch on a
    well-formed contract instead of having to check for None on both
    fields. A model that returns both or neither raises ValidationError
    and the loop surfaces it to the LLM as a recoverable error.
    """

    answer: str | None = None
    tool_call: ToolCall | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def either_answer_or_tool(self) -> DecisionOutput:
        a = self.answer is not None
        t = self.tool_call is not None
        if a == t:
            raise ValueError(
                "DecisionOutput requires exactly one of {answer, tool_call} "
                f"(got answer={'set' if a else 'None'}, tool_call={'set' if t else 'None'})"
            )
        return self


# ---------------------------------------------------------------------------
# Internal bookkeeping (not exchanged with the LLM)
# ---------------------------------------------------------------------------


class ActionResult(BaseModel):
    """One tool dispatch outcome.

    When the tool returns a large payload (above ARTIFACT_THRESHOLD_BYTES
    in action.py), Action offloads it to the artifact store and sets
    `artifact_id`. In that case `result` holds a short preview / descriptor
    so the agent's iteration history stays small.
    """

    tool: str
    arguments: dict[str, Any]
    status: Literal["ok", "error"]
    result: Any | None = None              # JSON-able payload OR a preview string
    artifact_id: str | None = None         # set when offloaded to artifact store
    error: str | None = None
    retried: bool = False
    duration_ms: int

    model_config = ConfigDict(extra="forbid")


class RunRecord(BaseModel):
    """One /agent6 invocation, for return value + state/runs index."""

    run_id: str
    started_at: datetime
    ended_at: datetime | None = None
    user_query: str
    observation: Observation | None = None
    iterations: int = 0
    final_answer: str | None = None
    status: Literal["running", "ok", "iteration_cap", "failed"] = "running"
    error: str | None = None

    model_config = ConfigDict(extra="forbid")


class TraceEvent(BaseModel):
    """One JSONL line in state/runs/<run_id>.jsonl.

    `layer` identifies which cognitive layer (or the loop driver) emitted
    the event; `payload` is the model_dump of whatever that layer produced
    that turn. Optionally tagged with `goal_id` to scope per-goal events.
    """

    timestamp: datetime
    iteration: int
    layer: Literal["perception", "memory.read", "memory.remember", "memory.write",
                   "decision", "action", "attach", "final"]
    goal_id: str | None = None
    payload: dict[str, Any]

    model_config = ConfigDict(extra="forbid")
