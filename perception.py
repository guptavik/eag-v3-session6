"""Perception layer — decomposes the user query into goals at iter 1
and refreshes goal state (done flags + artifact attachments) on
every subsequent iteration.

Two entry points:

- `observe()` is called every iteration. On iter 1 (no prior observation),
  it runs the LLM goal-decomposer. On iter 2+, it takes the prior
  observation, recent action results, and memory hits, and emits a
  refreshed observation with updated `done` flags + `attach_artifact_id`.

- `force_attach()` is the safety net for synthesis goals — if the
  current open goal contains "synthesise", "extract", "list",
  "compare", or "decide" and memory has at least one artifact, the
  most recent artifact is attached automatically. Documented in
  the assignment trace as the "force-attach for synthesis goals" net.

The done-marking rule on refresh: a goal is `done=True` when either
(a) the LLM refresh says so (LLM looks at the state and decides),
or (b) the loop has already marked it via Decision returning an
`answer` for that goal in a prior iteration (sticky-done).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import _gateway_path  # noqa: F401  — side-effect: adds mcp-server/ to sys.path
from llm_gatewayV3.client import LLM

from schemas import (
    ActionResult,
    Goal,
    MemoryItem,
    Observation,
    new_goal_id,
)

log = logging.getLogger(__name__)

# Keywords that mark a goal as "synthesis-shaped" — i.e. its job is to
# read prior artifacts and produce an answer. When the current open
# goal matches and there's an artifact in memory hits, force-attach
# kicks in. Order doesn't matter; matched case-insensitively.
SYNTHESIS_KEYWORDS = (
    "synthesise", "synthesize", "summarise", "summarize",
    "extract", "list",
    "compare", "decide", "choose", "pick", "select",
    "answer", "tell me",
)


class Perception:
    """LLM-driven goal decomposer + per-iter state refresher."""

    def __init__(self, llm: LLM | None = None) -> None:
        # Lazy LLM so unit tests of force_attach() / done-tracking can
        # run without a gateway.
        self._llm = llm

    # ------------------------------------------------------------------
    # System prompts — exported so they can be inspected in the README
    # ("Perception and Decision Prompt and Validation JSON of PoP" deliverable).
    # ------------------------------------------------------------------

    INITIAL_SYSTEM_PROMPT = (
        "You are the perception layer of a tool-using agent. Given the user's "
        "query and any prior memory the agent has, decompose the request into "
        "an ordered list of GOALS. Each goal must be one concrete unit of work "
        "that can be solved by either calling a tool or by composing prior "
        "results.\n\n"
        "Guidelines:\n"
        "1. Keep goal text short and imperative (\"Fetch the Wikipedia page for X\", "
        "\"Extract X from the fetched page\", \"Choose the best option given X\").\n"
        "2. The LAST goal must be the one whose answer is shown to the user.\n"
        "3. If a fact in memory already answers the query, emit a single "
        "synthesis goal — do not request a tool call you don't need.\n"
        "4. Do NOT decide which tool to use. Just describe what each goal needs.\n"
        "5. Number of goals: 1 for simple queries, 2-4 for multi-step.\n\n"
        "Return JSON: {\"goals\": [{\"text\": \"...\"}]}. "
        "Nothing else, no markdown, no commentary."
    )

    REFRESH_SYSTEM_PROMPT = (
        "You are the perception layer reviewing progress on an in-flight agent run. "
        "Given the prior goals and the most recent tool outcomes, decide for each "
        "goal whether it is now DONE. Be conservative: only mark a goal done if "
        "the evidence in the recent action results actually fulfils it.\n\n"
        "Rules:\n"
        "1. Goals already marked done STAY done (sticky-done invariant).\n"
        "2. A goal whose text says \"fetch X\" becomes done once a successful "
        "fetch_url result for X appears in the recent actions.\n"
        "3. A goal whose text says \"search for X\" becomes done once a successful "
        "web_search result for X appears.\n"
        "4. A goal whose text says \"answer\", \"choose\", \"extract\", \"list\", "
        "\"compare\", \"summarise\" etc. is NEVER done by a tool call alone — it "
        "is only marked done when the synthesis happens (which is the Decision "
        "layer's job, not yours).\n\n"
        "Return JSON: {\"goals\": [{\"id\": \"goal:xxx\", \"text\": \"...\", "
        "\"done\": true|false}]}. Preserve goal `id` and `text` exactly as given. "
        "Do not add or remove goals."
    )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def observe(
        self,
        *,
        user_query: str,
        memory_hits: list[MemoryItem],
        prior: Observation | None,
        recent_actions: list[ActionResult],
    ) -> Observation:
        """Single entry point used by agent6.py every iteration.

        If `prior` is None: this is iter 1 — run the LLM decomposer.
        If `prior` is set: run the LLM refresher to update `done` flags
        based on `recent_actions`. The refresher never changes goal
        `id` or `text` — that would break sticky-done.
        """
        if prior is None:
            return self._initial(user_query, memory_hits)
        return self._refresh(prior, recent_actions)

    def force_attach(
        self,
        *,
        observation: Observation,
        memory_hits: list[MemoryItem],
    ) -> Observation:
        """Safety net: for the first open synthesis-shaped goal,
        auto-attach the most-recent artifact found in memory.

        Mutates `observation.goals[i].attach_artifact_id` only when:
          - the first open goal's text contains a synthesis keyword
          - that goal does NOT already have an attach_artifact_id
          - memory_hits contains at least one MemoryItem with artifact_id

        Picks the artifact by recency (latest `created_at`). Documented
        in the assignment trace as the "force-attach for synthesis
        goals" net.
        """
        first_open = next((g for g in observation.goals if not g.done), None)
        if first_open is None:
            return observation
        if first_open.attach_artifact_id is not None:
            return observation
        if not _is_synthesis_goal(first_open.text):
            return observation
        artifact_id = _pick_latest_artifact(memory_hits)
        if artifact_id is None:
            return observation
        first_open.attach_artifact_id = artifact_id
        return observation

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _initial(
        self,
        user_query: str,
        memory_hits: list[MemoryItem],
    ) -> Observation:
        llm = self._ensure_llm()
        prompt = self._build_initial_prompt(user_query, memory_hits)
        # response_format dropped intentionally: see decision.py for the
        # gateway-vs-Gemini schema-translation gap. We parse the JSON
        # in _json_from_text below and fall back to the user_query as
        # a single goal on malformed output.
        resp = llm.chat(
            prompt=prompt,
            system=self.INITIAL_SYSTEM_PROMPT,
            auto_route="perception",
            temperature=1.0,
            max_tokens=512,
        )
        parsed = resp.get("parsed") or _json_from_text(resp.get("text", ""))
        if not parsed or not isinstance(parsed.get("goals"), list):
            # Fallback: single-goal observation echoing the user query.
            log.warning("perception: malformed initial JSON, falling back to single goal")
            return Observation(goals=[Goal(id=new_goal_id(), text=user_query.strip())])
        goals: list[Goal] = []
        for entry in parsed["goals"]:
            text = (entry or {}).get("text", "").strip()
            if not text:
                continue
            goals.append(Goal(id=new_goal_id(), text=text))
        if not goals:
            goals = [Goal(id=new_goal_id(), text=user_query.strip())]
        return Observation(goals=goals)

    def _refresh(
        self,
        prior: Observation,
        recent_actions: list[ActionResult],
    ) -> Observation:
        # Sticky-done: any goal already done in `prior` stays done.
        # If there's only one goal or all goals are already done, no
        # LLM call needed.
        if all(g.done for g in prior.goals):
            return prior
        # If there are no recent actions to evaluate against, just
        # echo back the prior state — refreshing with no new evidence
        # would be a no-op LLM call.
        if not recent_actions:
            return prior

        llm = self._ensure_llm()
        prompt = self._build_refresh_prompt(prior, recent_actions)
        resp = llm.chat(
            prompt=prompt,
            system=self.REFRESH_SYSTEM_PROMPT,
            auto_route="perception",
            temperature=1.0,
            max_tokens=512,
        )
        parsed = resp.get("parsed") or _json_from_text(resp.get("text", ""))
        if not parsed or not isinstance(parsed.get("goals"), list):
            log.warning("perception: malformed refresh JSON, keeping prior observation")
            return prior

        by_id = {g.id: g for g in prior.goals}
        for entry in parsed["goals"]:
            gid = (entry or {}).get("id")
            if gid not in by_id:
                continue
            new_done = bool(entry.get("done", False))
            # Sticky-done: never flip True→False.
            by_id[gid].done = by_id[gid].done or new_done

        return prior

    # ------------------------------------------------------------------

    def _build_initial_prompt(
        self, user_query: str, memory_hits: list[MemoryItem]
    ) -> str:
        parts = [f"User query:\n\"\"\"\n{user_query.strip()}\n\"\"\""]
        if memory_hits:
            descs = "\n".join(f"- {h.kind}: {h.descriptor}" for h in memory_hits[:5])
            parts.append(f"\nRelevant prior memory (top {min(5, len(memory_hits))}):\n{descs}")
        parts.append("\nReturn the JSON object.")
        return "\n".join(parts)

    def _build_refresh_prompt(
        self, prior: Observation, recent_actions: list[ActionResult]
    ) -> str:
        goals_blob = json.dumps(
            [{"id": g.id, "text": g.text, "done": g.done} for g in prior.goals]
        )
        # Keep the action summaries short — descriptors + status.
        action_lines: list[str] = []
        for a in recent_actions[-5:]:
            desc = a.result if isinstance(a.result, str) else json.dumps(a.result, default=str)
            if isinstance(desc, str) and len(desc) > 240:
                desc = desc[:240] + "…"
            action_lines.append(
                f"- {a.tool}({json.dumps(a.arguments, default=str)[:120]}) "
                f"→ {a.status}"
                + (f" artifact={a.artifact_id}" if a.artifact_id else "")
                + (f" error={a.error}" if a.error else "")
                + (f" : {desc}" if a.status == "ok" else "")
            )
        actions_blob = "\n".join(action_lines) if action_lines else "(none)"
        return (
            f"Prior goals:\n{goals_blob}\n\n"
            f"Most recent actions:\n{actions_blob}\n\n"
            "Return the JSON object with updated `done` flags."
        )

    def _ensure_llm(self) -> LLM:
        if self._llm is None:
            self._llm = LLM()
        return self._llm


# ---------------------------------------------------------------------------
# Helpers — module level so they're unit-testable without the LLM.
# ---------------------------------------------------------------------------


def _is_synthesis_goal(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in SYNTHESIS_KEYWORDS)


def _pick_latest_artifact(memory_hits: list[MemoryItem]) -> str | None:
    """Among memory hits with an `artifact_id`, return the most-recently
    created one's id. Returns None if no hit has an artifact."""
    with_art = [h for h in memory_hits if h.artifact_id]
    if not with_art:
        return None
    with_art.sort(key=lambda h: h.created_at, reverse=True)
    return with_art[0].artifact_id


_INITIAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["goals"],
    "additionalProperties": False,
}


_REFRESH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "done": {"type": "boolean"},
                },
                "required": ["id", "text", "done"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["goals"],
    "additionalProperties": False,
}


def _json_from_text(text: str) -> dict[str, Any] | None:
    """Parse a model's text output as JSON, tolerating code fences."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None
