# Prompt Qualification (Session 5)

This document captures how the agent's system prompt was scored against the **Prompt Evaluation Assistant** rubric, before and after the Session 5 upgrade. The rubric scores nine criteria — the goal of this upgrade was to move every criterion from a `false` / partial to a `true`.

## The evaluator rubric

The evaluator (a separate LLM prompted to act as a "Prompt Evaluation Assistant") scores a prompt across nine criteria and returns a JSON verdict:

```
1. Explicit Reasoning Instructions    — does the prompt say "think step-by-step"?
2. Structured Output Format           — is output predictable / parseable?
3. Separation of Reasoning and Tools  — reasoning vs. tool-use kept distinct?
4. Conversation Loop Support          — works in multi-turn?
5. Instructional Framing              — examples of desired behavior?
6. Internal Self-Checks               — model is told to sanity-check itself?
7. Reasoning Type Awareness           — tags type of reasoning (logic, lookup, …)?
8. Error Handling or Fallbacks        — what to do when uncertain / tool fails?
9. Overall Clarity and Robustness     — reduces hallucination and drift?
```

---

## Before (Session 4 prompt)

The Session 4 prompt is preserved verbatim in [the git history](../api.js) (see `git log api.js`). It described the five tools, the operating rules, and the markdown brief format — but it did not name reasoning types, did not require a self-check step, and gave no worked example.

**Evaluator verdict (run by Claude acting as the Prompt Evaluation Assistant):**

```json
{
  "explicit_reasoning": false,
  "structured_output": true,
  "tool_separation": false,
  "conversation_loop": true,
  "instructional_framing": true,
  "internal_self_checks": false,
  "reasoning_type_awareness": false,
  "fallbacks": true,
  "overall_clarity": "Clear and well-organized for the brief output format, but lacks explicit step-by-step reasoning, structured separation between reasoning and tool calls, internal self-checks, and reasoning-type tagging. The agent is told what to do but not how to think before doing it."
}
```

**Score: 4 of 8 boolean criteria true.** Gaps: explicit reasoning, reasoning↔tool separation, self-checks, reasoning-type awareness.

---

## After (Session 5 prompt — current `SYSTEM_PROMPT` in [api.js](../api.js))

The upgraded prompt adds two explicit, machine-readable sections on top of the existing **Operating rules** and **Final response format** blocks:

- **`Self-check rules`** — three gates the model runs at three points in the loop:
  1. *After fetching meetings* — confirm at least one meeting matched, otherwise stop.
  2. *After profiling attendees / searching email* — verify returned data is non-empty and on-topic; note gaps explicitly.
  3. *Before writing the brief* — confirm core fields (title, time, ≥1 attendee or agenda item) exist, or flag them under a `⚠️ Missing Context` section.
- **`Reasoning transparency rules`** — every plan line tags the reasoning type as `[LOOKUP]`, `[SYNTHESIS]`, `[SCHEDULING]`, `[SEARCH]`, or `[PROFILE]`; sections built on incomplete data must annotate their confidence inline.

**Evaluator verdict (same evaluator, run against the new prompt):**

```json
{
  "explicit_reasoning": true,
  "structured_output": true,
  "tool_separation": true,
  "conversation_loop": true,
  "instructional_framing": true,
  "internal_self_checks": true,
  "reasoning_type_awareness": true,
  "fallbacks": true,
  "overall_clarity": "Excellent — the prompt pairs concrete operating rules with a three-stage self-check gate (after fetch, after profiling/email, before brief) and a five-tag reasoning-type taxonomy ([LOOKUP], [SYNTHESIS], [SCHEDULING], [SEARCH], [PROFILE]) that the UI can render distinctly. The brief format separates content from tool-use guidance, and explicit inline-confidence rules + the ⚠️ Missing Context section dramatically reduce hallucination risk. Worked example in the transparency section makes the expected output unambiguous."
}
```

**Score: 8 of 8 boolean criteria true.**

---

## Per-criterion mapping

This table shows exactly which section of the new prompt addresses each criterion. Read alongside [api.js](../api.js) `SYSTEM_PROMPT`.

| # | Criterion | Where it's addressed in the new prompt |
|---|---|---|
| 1 | **Explicit reasoning instructions** | `Operating rules` — *"Plan before you act. Briefly state what you intend to do, then call the tool(s)."* `Reasoning transparency rules` — every tool call must be preceded by a tagged plan line. |
| 2 | **Structured output format** | `Final response format` defines the markdown brief schema; `Reasoning transparency rules` define the parseable `[TAG]` prefix the UI in [popup.js](../popup.js) splits and renders distinctly. |
| 3 | **Separation of reasoning and tools** | `Operating rules` — *"Plan before you act…then call the tool(s)."* Reasoning is in text; tools are in function-call blocks. |
| 4 | **Conversation loop support** | The agent loop is multi-turn by construction (see [agent.js](../agent.js)); the prompt's *"don't re-fetch what you already have"* discipline (in tool-use rules) carries facts forward. |
| 5 | **Instructional framing** | `Reasoning transparency rules` ships a worked example: *"Fetching calendar [LOOKUP] → then profiling attendees in parallel [PROFILE]"*. `Final response format` shows the full markdown brief skeleton. |
| 6 | **Internal self-checks** | `Self-check rules` — three explicit gates (after fetch / after profiling+email / before brief). Failed gates either stop the loop or render under a `⚠️ Missing Context` section. |
| 7 | **Reasoning type awareness** | `Reasoning transparency rules` lists five tags by cognitive type: `[LOOKUP]` (retrieval), `[SYNTHESIS]` (composition), `[SCHEDULING]` (calendar work), `[SEARCH]` (web/email), `[PROFILE]` (attendee profiling). |
| 8 | **Error handling / fallbacks** | `Operating rules` — *"If a tool returns no results or fails, adapt: try a different query, skip that step, or note the gap. Do not fabricate data."* Plus inline-confidence annotations and the `⚠️ Missing Context` section escape hatch. |
| 9 | **Overall clarity** | Three sections of rules (Operating / Self-check / Reasoning transparency) each with a single concern; brief format documented separately. Compact (no padding) and free of contradiction. |

---

## How to reproduce the evaluator run

The evaluator is a prompt, not a tool. To re-score either prompt yourself:

1. Open any LLM with a fresh context (ChatGPT, Claude, Cursor, Gemini).
2. Paste the evaluator's instructions (the "You are a Prompt Evaluation Assistant…" block from the Session 5 brief).
3. Paste the prompt being evaluated as the next message.
4. The LLM returns the JSON verdict.

The verdicts above were produced by running the evaluator against the exact `SYSTEM_PROMPT` strings in `api.js` at the Session 4 commit (`627e121`) and the Session 5 HEAD respectively.

---

## How the tagged blocks show up at runtime

When the agent emits a plan line like `Fetching calendar [LOOKUP] → then profiling attendees [PROFILE]`, [popup.js](../popup.js) in `splitTaggedBlocks()` recognizes each tag, picks the matching icon + color from `REASONING_TAGS`, and renders each segment as a colored collapsible row with a tag pill:

- 🔎 `LOOKUP`     — purple  *(retrieval — calendar / email / web)*
- ✍️ `SYNTHESIS`  — red     *(composing the brief)*
- 📅 `SCHEDULING` — blue    *(calendar-window math)*
- 🌐 `SEARCH`     — amber   *(web / email lookups)*
- 👤 `PROFILE`    — green   *(attendee background)*

This is the *visible* evidence the prompt is working: the reasoning chain in the UI is no longer one homogeneous stream of italicized thoughts but a structured sequence of typed steps, with each tool call attributed to the kind of cognitive work that motivated it.
