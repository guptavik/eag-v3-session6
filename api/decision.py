"""Decision layer — given the current open goal, decide whether to
call one more tool or emit the final answer for that goal.

The contract is enforced by `DecisionOutput`'s model_validator:
exactly one of `answer` (non-empty) or `tool_call` is set. The loop
in agent6.py refuses to dispatch anything that doesn't satisfy this.

Input to every Decision call:
  - the current open goal
  - the full observation (so the model sees the surrounding goals)
  - memory hits relevant to this goal
  - history of action results from this run
  - optional attached artifact text (when the goal has
    `attach_artifact_id` set — the loop loads the bytes and prepends
    them to the prompt)
  - the tool catalogue (names + descriptions + JSON schemas)

The decision call uses `auto_route="decision"` so the gateway routes
to the appropriate tier based on prompt size + complexity.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import _gateway_path  # noqa: F401  — side-effect: adds mcp-server/ to sys.path
from llm_gatewayV3.client import LLM

from schemas import (
    ActionResult,
    DecisionOutput,
    Goal,
    MemoryItem,
    Observation,
    ToolCall,
)

log = logging.getLogger(__name__)

# Above this size, we truncate the attached artifact's content in the
# Decision prompt. The artifact bytes can be hundreds of KB; the prompt
# routing tops out around 8 KB practical for free-tier Gemini, so we
# cap the attached blob at ~24 KB.
ATTACHED_MAX_CHARS = 24_000

# History tail length included in the Decision prompt — keeps recent
# tool outcomes visible without bloating context with old artifacts.
HISTORY_TAIL = 6


class Decision:
    """LLM-driven next-step picker. One call per iteration."""

    def __init__(self, llm: LLM | None = None) -> None:
        self._llm = llm

    # ------------------------------------------------------------------
    # System prompt — kept as a class constant so the README can quote
    # it verbatim ("Perception and Decision Prompt and Validation JSON
    # of PoP" deliverable).
    # ------------------------------------------------------------------

    SYSTEM_PROMPT = (
        "You are the decision layer of a tool-using agent. You see exactly "
        "one open GOAL at a time and must decide what to do next.\n\n"
        "Available actions on each turn:\n"
        " (a) emit a `tool_call` — when you need more information to make "
        "progress on the goal. Pick exactly one tool from the catalogue and "
        "provide JSON arguments matching its schema.\n"
        " (b) emit an `answer` — when the data you already have is enough "
        "to satisfy the current goal. The answer must be the human-readable "
        "text for this goal (this goal only — later goals get their own turn).\n\n"
        "Hard rules:\n"
        "1. Output MUST be exactly one JSON object with two keys: `answer` and "
        "`tool_call`. EXACTLY ONE of them is non-null; the other is null. "
        "Emit nothing else — no prose, no markdown fences, no commentary.\n"
        "2. Do not invent tool names. Use only the names listed in the "
        "Tool catalogue section of the user message.\n"
        "3. For goals shaped like \"fetch X\" or \"search for X\", emit a "
        "tool_call. For goals shaped like \"extract / list / choose / "
        "compare / answer\", emit an answer once the needed data is "
        "present (either in memory hits or in the ATTACHED ARTIFACT).\n"
        "4. If an artifact is ATTACHED below the prompt, read it and use "
        "its content — DO NOT call fetch_url for the same URL again.\n"
        "5. Never fabricate facts. If the data is genuinely missing, emit "
        "a tool_call to get it.\n"
        "6. When the current goal text contains a fully-qualified URL "
        "(https://… or http://…), prefer `fetch_url` over `web_search` — "
        "the page is already named, there is nothing to search for.\n"
        "7. When the goal text starts with \"Create a file\", \"Save\", "
        "\"Record\", or otherwise asks to persist data, emit a "
        "`create_file` tool_call with a sensible sandbox path "
        "(reminders/<slug>.txt, notes/<slug>.md, etc.) and a body that "
        "captures the fact. Use `update_file` only if the path already "
        "exists in memory hits or recent action results."
    )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def next(
        self,
        *,
        goal: Goal,
        observation: Observation,
        memory_hits: list[MemoryItem],
        history: list[ActionResult],
        attached_text: str | None,
        tool_catalogue: list[dict[str, Any]],
    ) -> DecisionOutput:
        """One Gemini call → one DecisionOutput. Raises ValueError if
        the model output can't be coerced into a valid DecisionOutput
        (the loop catches this and surfaces the error to the next
        iteration's perception)."""
        llm = self._ensure_llm()
        prompt = self._build_prompt(
            goal=goal,
            observation=observation,
            memory_hits=memory_hits,
            history=history,
            attached_text=attached_text,
            tool_catalogue=tool_catalogue,
        )
        # NOTE: We intentionally do NOT pass response_format here. The
        # gateway's schema-validation path is strict JSON Schema (rejects
        # OpenAPI's "nullable" keyword), but the Gemini worker only
        # accepts OpenAPI-style schemas (rejects JSON Schema union
        # types like {"type": ["string", "null"]}). Going through
        # response_format produces a 503 on either path depending on
        # which form we send. Instead, we get plain text out and parse
        # it ourselves; Pydantic's DecisionOutput validator still
        # enforces the XOR contract downstream — no regex involved.
        resp = llm.chat(
            prompt=prompt,
            system=self.SYSTEM_PROMPT,
            auto_route="decision",
            temperature=1.0,
            max_tokens=1200,
        )
        parsed = resp.get("parsed") or _json_from_text(resp.get("text", ""))
        if not parsed:
            raise ValueError(
                f"decision: model returned no parseable JSON (text={resp.get('text', '')[:200]!r})"
            )

        # Build a DecisionOutput; let the XOR validator surface mis-shaped
        # outputs as ValueError so the loop can react.
        tc_raw = parsed.get("tool_call")
        tool_call = (
            ToolCall(
                name=str(tc_raw.get("name", "")),
                arguments=dict(tc_raw.get("arguments") or {}),
            )
            if isinstance(tc_raw, dict) and tc_raw.get("name")
            else None
        )
        answer_raw = parsed.get("answer")
        answer = answer_raw.strip() if isinstance(answer_raw, str) and answer_raw.strip() else None
        return DecisionOutput(answer=answer, tool_call=tool_call)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        *,
        goal: Goal,
        observation: Observation,
        memory_hits: list[MemoryItem],
        history: list[ActionResult],
        attached_text: str | None,
        tool_catalogue: list[dict[str, Any]],
    ) -> str:
        sections: list[str] = []

        # 1. The full goals list, so Decision sees what comes next.
        goals_blob = "\n".join(
            f"  {i+1}. {'[done] ' if g.done else '[open] '}{g.text}"
            f"{(' (attach=' + g.attach_artifact_id + ')') if g.attach_artifact_id else ''}"
            for i, g in enumerate(observation.goals)
        )
        sections.append(f"All goals:\n{goals_blob}")

        # 2. Highlight the CURRENT goal Decision is solving for.
        sections.append(f"\nCURRENT GOAL ({goal.id}):\n  {goal.text}")

        # 3. Tool catalogue.
        tool_lines: list[str] = []
        for t in tool_catalogue:
            schema_preview = json.dumps(t.get("input_schema") or {}, default=str)
            if len(schema_preview) > 320:
                schema_preview = schema_preview[:320] + "…"
            tool_lines.append(
                f"  - {t.get('name')}: {t.get('description', '')}\n"
                f"      schema: {schema_preview}"
            )
        sections.append("\nTool catalogue:\n" + "\n".join(tool_lines))

        # 4. Memory hits (descriptors only — don't echo full payloads).
        if memory_hits:
            mem_lines = "\n".join(
                f"  - {h.kind}: {h.descriptor}"
                + (f" (artifact_id={h.artifact_id})" if h.artifact_id else "")
                for h in memory_hits[:8]
            )
            sections.append(f"\nMemory hits ({len(memory_hits)}):\n{mem_lines}")

        # 5. Action history (tail). Each row: tool, args, status, short summary.
        if history:
            hist_lines: list[str] = []
            for r in history[-HISTORY_TAIL:]:
                desc = r.result if isinstance(r.result, str) else json.dumps(r.result, default=str)
                if isinstance(desc, str) and len(desc) > 320:
                    desc = desc[:320] + "…"
                hist_lines.append(
                    f"  - {r.tool}({json.dumps(r.arguments, default=str)[:160]}) "
                    f"→ {r.status}"
                    + (f" artifact={r.artifact_id}" if r.artifact_id else "")
                    + (f" error={r.error}" if r.error else "")
                    + (f"\n      result: {desc}" if r.status == "ok" and desc else "")
                )
            sections.append(f"\nAction history (last {min(HISTORY_TAIL, len(history))}):\n" + "\n".join(hist_lines))

        # 6. Attached artifact bytes (truncated to the cap).
        if attached_text:
            attached = attached_text
            if len(attached) > ATTACHED_MAX_CHARS:
                attached = attached[:ATTACHED_MAX_CHARS] + f"\n…[truncated, original was {len(attached_text)} chars]"
            sections.append(
                f"\nATTACHED ARTIFACT ({goal.attach_artifact_id}, "
                f"{len(attached_text)} chars):\n---\n{attached}\n---"
            )

        sections.append(
            "\nDecide: return the JSON object now. "
            "Exactly one of answer | tool_call must be populated."
        )
        return "\n".join(sections)

    def _ensure_llm(self) -> LLM:
        if self._llm is None:
            self._llm = LLM()
        return self._llm


def _json_from_text(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    # Strip code fences using plain string ops (no regex on LLM output).
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1 :] if nl != -1 else text[3:]
        text = text.rstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()
    # Some providers prepend a single tag line (e.g. "[LOOKUP] fetching …")
    # before the JSON. Locate the first { and parse from there.
    brace = text.find("{")
    if brace > 0:
        text = text[brace:]
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except json.JSONDecodeError:
        return None
