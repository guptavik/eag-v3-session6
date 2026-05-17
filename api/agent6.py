"""agent6 — the loop that wires Perception → Memory → Decision → Action.

Run with:
    uv run python agent6.py "Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth and death dates."

Or programmatically:
    from agent6 import run
    record = await run("What's the time in Tokyo?")

The loop's invariants:

1. **memory.remember()** is called once at the top, before iter 1.
   Query C run 1 depends on this — the user's declarative statement
   must be persisted before Decision is consulted.

2. **memory.read()** runs every iteration, providing fresh hits to
   Perception. Memory accumulates: each tool_outcome the loop
   writes shows up on the next iteration's read.

3. **Perception** is called every iteration. On iter 1 it decomposes
   the user query into goals; on later iters it refreshes done flags.
   The force-attach safety net runs after refresh, before Decision.

4. **Decision** is called once per iteration for the first open goal.
   It either emits an `answer` (closes the goal) or a `tool_call`
   (Action dispatches it; result becomes a memory item).

5. **Iteration cap**: MAX_ITERATIONS per run. Twice the expected
   iteration count of the hardest target query keeps the cap
   honest — if the agent hits this, the run failed.

6. **Trace** is one JSONL file per run under state/runs/. Each line is
   a TraceEvent. The trace is also pretty-printed to stdout in the
   style of the assignment's reference traces.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# Trace lines use box-drawing chars (─, →, …) which the default Windows
# console (cp1252) can't encode. Force UTF-8 on stdout/stderr before
# anything else prints. line_buffering=True also makes the trace appear
# in real time when stdout is piped (e.g. `... | tail` or to a file),
# instead of buffering until the process exits and looking "stuck".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

from dotenv import load_dotenv

from action import Action
from decision import Decision
from memory import AgentMemory, ArtifactStore, _keywords_from_text
from perception import Perception
from schemas import (
    ActionResult,
    DecisionOutput,
    Goal,
    MemoryItem,
    Observation,
    RunRecord,
    ToolCall,
    TraceEvent,
    new_memory_id,
    new_run_id,
)

log = logging.getLogger(__name__)

# --- Runtime constants ----------------------------------------------

ROOT = Path(__file__).parent
SERVER_SCRIPT = ROOT / "mcp_server.py"
STATE_DIR = ROOT / "state"
MEMORY_PATH = STATE_DIR / "memory.json"
ARTIFACTS_DIR = STATE_DIR / "artifacts"
RUNS_DIR = STATE_DIR / "runs"

# Twice the largest expected iteration count from the assignment
# (Query D: 5-7 → 14 leaves clear headroom).
MAX_ITERATIONS = 16


# ====================================================================
# Tracer — JSONL file + pretty stdout in the style of the assignment.
# ====================================================================


class Tracer:
    """Writes per-iteration events to state/runs/<run_id>.jsonl and
    a human-readable trace to stdout. The stdout format intentionally
    mirrors the assignment's reference trace headers so output is
    comparable line-for-line."""

    def __init__(self, run_id: str) -> None:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._jsonl = (RUNS_DIR / f"{run_id}.jsonl").open("a", encoding="utf-8")
        self.iteration = 0

    def close(self) -> None:
        try:
            self._jsonl.close()
        except Exception:
            pass

    def begin_iteration(self, n: int) -> None:
        self.iteration = n
        print(f"\n─── iter {n} ───")

    def record(
        self,
        layer: str,
        payload: Any,
        *,
        goal_id: str | None = None,
    ) -> None:
        if hasattr(payload, "model_dump"):
            payload_dict = payload.model_dump(mode="json")
        elif isinstance(payload, dict):
            payload_dict = payload
        else:
            payload_dict = {"value": str(payload)}
        ev = TraceEvent(
            timestamp=dt.datetime.now(dt.timezone.utc),
            iteration=self.iteration,
            layer=layer,  # type: ignore[arg-type]
            goal_id=goal_id,
            payload=payload_dict,
        )
        self._jsonl.write(ev.model_dump_json() + "\n")
        self._jsonl.flush()

    # ----- pretty printers --------------------------------------

    def print_memory_read(self, hits: list[MemoryItem]) -> None:
        print(f"[memory.read]   {len(hits)} hits")
        for h in hits[:3]:
            print(f"                {h.kind}: {h.descriptor}")

    def print_perception(self, obs: Observation) -> None:
        print("[perception]    " + _format_goals(obs.goals))

    def print_attach(self, artifact_id: str, size_bytes: int) -> None:
        print(f"[attach]        {artifact_id} ({size_bytes} bytes)")

    def print_decision(self, d: DecisionOutput) -> None:
        if d.tool_call is not None:
            args = json.dumps(d.tool_call.arguments, default=str)
            print(f"[decision]      TOOL_CALL: {d.tool_call.name}({args})")
        elif d.answer is not None:
            preview = d.answer.replace("\n", " ")
            if len(preview) > 260:
                preview = preview[:260] + "…"
            print(f"[decision]      ANSWER: {preview}")

    def print_action(self, result: ActionResult) -> None:
        if result.status == "ok" and result.artifact_id:
            preview = (
                result.result if isinstance(result.result, str) else json.dumps(result.result, default=str)
            )
            if isinstance(preview, str) and len(preview) > 80:
                preview = preview[:80] + "…"
            print(
                f"[action]        → [artifact {result.artifact_id}, "
                f"see memory] preview: {preview}"
            )
        elif result.status == "ok":
            preview = (
                result.result
                if isinstance(result.result, str)
                else json.dumps(result.result, default=str)
            )
            if isinstance(preview, str) and len(preview) > 160:
                preview = preview[:160] + "…"
            print(f"[action]        → {preview}")
        else:
            print(f"[action]        → error: {result.error}")

    def print_remember(self, items: list[MemoryItem]) -> None:
        if not items:
            return
        for it in items:
            kws = ", ".join(it.keywords)
            print(f"[memory.remember]  classified {it.descriptor!r} as {it.kind}")
            print(f"                   keywords: [{kws}]")


def _format_goals(goals: list[Goal]) -> str:
    lines = []
    for i, g in enumerate(goals):
        marker = "[done]" if g.done else "[open]"
        line = f"{marker} {g.text}"
        if i == 0:
            lines.append(line)
        else:
            lines.append(f"                {line}")
        if g.attach_artifact_id and not g.done:
            lines.append(f"                  attach={g.attach_artifact_id}")
    return "\n".join(lines)


# ====================================================================
# The loop
# ====================================================================


async def run(user_query: str, *, max_iterations: int = MAX_ITERATIONS) -> RunRecord:
    """Run agent6 on `user_query`. Returns a RunRecord with the final
    answer + iteration count + status."""
    load_dotenv(ROOT / ".env")
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    run_id = new_run_id()
    started_at = dt.datetime.now(dt.timezone.utc)
    tracer = Tracer(run_id)

    artifacts = ArtifactStore(ARTIFACTS_DIR)
    memory = AgentMemory(MEMORY_PATH)
    perception = Perception()
    decision = Decision()

    record = RunRecord(
        run_id=run_id,
        started_at=started_at,
        user_query=user_query,
        status="running",
    )

    # --- pre-loop: remember anything declarative the user just stated
    try:
        remembered = memory.remember(user_query, run_id=run_id)
        if remembered:
            tracer.print_remember(remembered)
            tracer.record("memory.remember", {"items": [it.model_dump(mode="json") for it in remembered]})
    except Exception as exc:
        log.warning("memory.remember failed (non-fatal): %s", exc)

    # --- start the MCP transport
    action = Action(server_script=SERVER_SCRIPT, artifacts=artifacts)
    await action.start()
    try:
        tool_catalogue = await _fetch_tool_catalogue(action)
        record.observation, record.iterations, record.final_answer, record.status = await _loop(
            user_query=user_query,
            run_id=run_id,
            memory=memory,
            perception=perception,
            decision=decision,
            action=action,
            artifacts=artifacts,
            tool_catalogue=tool_catalogue,
            tracer=tracer,
            max_iterations=max_iterations,
        )
    finally:
        await action.stop()
        tracer.close()

    record.ended_at = dt.datetime.now(dt.timezone.utc)

    print()
    if record.status == "ok":
        print(f"[done] all {len(record.observation.goals)} goals satisfied")  # type: ignore[union-attr]
        print()
        print(f"FINAL: {record.final_answer}")
    elif record.status == "iteration_cap":
        print(f"[stopped] iteration cap ({max_iterations}) hit")
    else:
        print(f"[failed] {record.status}")

    return record


async def _loop(
    *,
    user_query: str,
    run_id: str,
    memory: AgentMemory,
    perception: Perception,
    decision: Decision,
    action: Action,
    artifacts: ArtifactStore,
    tool_catalogue: list[dict[str, Any]],
    tracer: Tracer,
    max_iterations: int,
) -> tuple[Observation | None, int, str | None, str]:
    """Returns (final observation, iteration count, final answer, status)."""
    observation: Observation | None = None
    final_answer: str | None = None
    history: list[ActionResult] = []
    last_actions_for_perception: list[ActionResult] = []

    # Bail out if Decision raises this many times in a row. The agent
    # cannot make progress without a working Decision call, and looping
    # to the iteration cap on a deterministic failure (e.g. a malformed
    # response_schema rejected by the worker) only wastes tokens.
    consecutive_decision_errors = 0
    MAX_CONSECUTIVE_DECISION_ERRORS = 3

    query_keywords = _keywords_from_text(user_query)

    for iteration in range(1, max_iterations + 1):
        tracer.begin_iteration(iteration)

        # 1. Memory read
        # Include the keywords from each open goal as well, so memory
        # recall picks up tool_outcomes that match goal-specific terms
        # (e.g. "wikipedia", "tokyo") on top of the user-query terms.
        recall_keywords = list(query_keywords)
        if observation:
            for g in observation.goals:
                if not g.done:
                    recall_keywords.extend(_keywords_from_text(g.text))
        memory_hits = memory.recall(recall_keywords, limit=15)
        tracer.print_memory_read(memory_hits)
        tracer.record("memory.read", {"hits": len(memory_hits)})

        # 2. Perception (initial decomposition or refresh)
        observation = perception.observe(
            user_query=user_query,
            memory_hits=memory_hits,
            prior=observation,
            recent_actions=last_actions_for_perception,
        )

        # 3. Force-attach safety net for synthesis goals
        observation = perception.force_attach(
            observation=observation,
            memory_hits=memory_hits,
        )

        tracer.print_perception(observation)
        tracer.record("perception", observation)

        # Done?
        if all(g.done for g in observation.goals):
            status = "ok" if final_answer else "ok"  # answer might be on last goal already
            return observation, iteration, final_answer, status

        current_goal = next(g for g in observation.goals if not g.done)

        # 4. If the goal has an attached artifact, load its bytes.
        attached_text: str | None = None
        if current_goal.attach_artifact_id:
            try:
                meta = artifacts.read_meta(current_goal.attach_artifact_id)
                attached_text = artifacts.read_text(current_goal.attach_artifact_id)
                tracer.print_attach(current_goal.attach_artifact_id, meta.size_bytes)
                tracer.record(
                    "attach",
                    {"artifact_id": current_goal.attach_artifact_id, "size_bytes": meta.size_bytes},
                    goal_id=current_goal.id,
                )
            except FileNotFoundError as exc:
                log.warning("attach: %s — proceeding without artifact", exc)
                attached_text = None

        # 5. Decision
        try:
            decision_output = decision.next(
                goal=current_goal,
                observation=observation,
                memory_hits=memory_hits,
                history=history,
                attached_text=attached_text,
                tool_catalogue=tool_catalogue,
            )
        except Exception as exc:
            log.error("decision: %s", exc)
            tracer.record("decision", {"error": str(exc)}, goal_id=current_goal.id)
            consecutive_decision_errors += 1
            if consecutive_decision_errors >= MAX_CONSECUTIVE_DECISION_ERRORS:
                log.error(
                    "decision: %d consecutive failures — bailing out",
                    consecutive_decision_errors,
                )
                return observation, iteration, final_answer, "failed"
            # Surface as a memory item so the next iteration sees the error.
            memory.add(
                MemoryItem(
                    id=new_memory_id(),
                    kind="scratchpad",
                    keywords=_keywords_from_text(current_goal.text),
                    descriptor=f"decision error on {current_goal.text!r}: {exc}",
                    value={"error": str(exc)},
                    artifact_id=None,
                    source="agent6.decision",
                    run_id=run_id,
                    goal_id=current_goal.id,
                    confidence=1.0,
                    created_at=dt.datetime.now(dt.timezone.utc),
                )
            )
            last_actions_for_perception = []
            continue

        consecutive_decision_errors = 0  # reset on a successful decision
        tracer.print_decision(decision_output)
        tracer.record("decision", decision_output, goal_id=current_goal.id)

        if decision_output.answer is not None:
            # Goal closed by Decision.
            current_goal.done = True
            if current_goal is observation.goals[-1]:
                final_answer = decision_output.answer
            last_actions_for_perception = []
            continue

        # Decision wants a tool. Dispatch.
        assert decision_output.tool_call is not None  # XOR enforced
        result = await action.execute(decision_output.tool_call)
        tracer.print_action(result)
        tracer.record("action", result, goal_id=current_goal.id)
        history.append(result)
        last_actions_for_perception = [result]

        # Record the action result as a memory item so future iterations'
        # memory.read() and perception.force_attach() can find it.
        memory.add(
            _action_to_memory(result, run_id=run_id, goal_id=current_goal.id)
        )

    # Iteration cap exhausted.
    return observation, max_iterations, final_answer, "iteration_cap"


# ====================================================================
# Helpers
# ====================================================================


async def _fetch_tool_catalogue(action: Action) -> list[dict[str, Any]]:
    """Pull the tool list from the live MCP session and reshape into
    the descriptor format Decision expects."""
    assert action._session is not None  # noqa: SLF001
    listing = await action._session.list_tools()  # noqa: SLF001
    out: list[dict[str, Any]] = []
    for t in listing.tools:
        out.append(
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema or {},
            }
        )
    return out


def _action_to_memory(result: ActionResult, *, run_id: str, goal_id: str) -> MemoryItem:
    """Convert one tool outcome into a MemoryItem so it shows up in
    future recall + force-attach scans."""
    desc = (
        f"{result.tool} → ok"
        if result.status == "ok"
        else f"{result.tool} → error: {result.error}"
    )
    # Keywords: tool name + the words from the arguments + the result
    # descriptor (which often contains the URL / query / path).
    kws = set([result.tool])
    for v in (result.arguments or {}).values():
        if isinstance(v, str):
            kws.update(_keywords_from_text(v))
    if isinstance(result.result, str):
        kws.update(_keywords_from_text(result.result))
    return MemoryItem(
        id=new_memory_id(),
        kind="tool_outcome",
        keywords=sorted(k for k in kws if k),
        descriptor=desc,
        value={
            "tool": result.tool,
            "arguments": result.arguments,
            "status": result.status,
            "result": result.result,
            "error": result.error,
            "artifact_id": result.artifact_id,
            "retried": result.retried,
            "duration_ms": result.duration_ms,
        },
        artifact_id=result.artifact_id,
        source=f"tool:{result.tool}",
        run_id=run_id,
        goal_id=goal_id,
        confidence=1.0 if result.status == "ok" else 0.5,
        created_at=dt.datetime.now(dt.timezone.utc),
    )


# ====================================================================
# CLI entry
# ====================================================================


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Silence the noisier libraries when we're not in debug mode.
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("mcp").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="agent6 — 4-layer cognitive agent")
    parser.add_argument("query", help="The user query to run")
    parser.add_argument("--max-iter", type=int, default=MAX_ITERATIONS, help="Iteration cap")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    asyncio.run(run(args.query, max_iterations=args.max_iter))


if __name__ == "__main__":
    main()
