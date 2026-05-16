# Session 6 — Agent6 Implementation Plan

This document plans the **new** Session 6 assignment: a 4-layer cognitive agent (perception → memory → decision → action) backed by the LLM Gateway V3 substrate, talking to a stdio MCP server with 9 general-purpose tools, persisting state to disk, and answering 4 specific target queries within bounded iteration counts.

This is a planning artifact only. **No code is written until this plan is approved.**

---

## 0. Status of the existing repo

The current `eag-v3-session6/` repo contains the Meeting Intelligence Agent — Chrome extension + Python FastMCP HTTP server with 5 calendar/email/web tools, plus the multi-agent runtime we just landed.

The new assignment is a **different agent** with **different tools** and a **stdio** transport. The provided `mcp_server.py` exposes web_search, fetch_url, get_time, currency_convert, and 5 file operations under `./sandbox/` — there is no calendar / Gmail / SerpAPI / Gemini code in it.

### Open question: what happens to the meeting-intelligence code?

| Option | What it means | Pros | Cons |
|---|---|---|---|
| **A. Park it** (recommended) | Move `mcp-server/` → `mcp-server-meeting-intel/`, the Chrome extension files (`popup.*`, `agent-client.js`, `manifest.json`, `styles.css`, `icons/`, `docs/`) → `extension-meeting-intel/`. Old work stays as a sibling, the new assignment occupies the repo root. | Preserves the prior session work for reference; clean repo root for the new assignment; nothing has to be deleted | One extra rename commit; README has to mention both projects |
| **B. Delete it** | Remove the meeting-intel files entirely | Cleanest repo root | Discards 4,000+ lines of prior work and Session-5 demo material |
| **C. Keep alongside** | Add the new code at the repo root next to the existing tree; no renames | Zero file motion | Two `mcp-server/` directories would collide; really not an option |

**Recommendation: Option A.** Reversible, preserves history, keeps the prior sessions runnable.

---

## 1. Target repo layout (after the move)

```
eag-v3-session6/
├── README.md                       # rewritten for the new assignment
├── PLAN.md                         # this file
├── pyproject.toml                  # uv-managed; deps: mcp, pydantic, httpx,
│                                   # ddgs, tavily, crawl4ai, python-dotenv
├── uv.lock
├── .env.example                    # TAVILY_API_KEY (optional) + LLM_GATEWAY_V3_URL
├── .python-version                 # 3.12
├── .gitignore                      # adds state/ and sandbox/
│
├── mcp_server.py                   # provided (9 tools, stdio transport) — verbatim
│
├── agent6.py                       # the loop — wires the 4 layers together
├── schemas.py                      # ALL Pydantic v2 contracts
├── perception.py                   # parse user query → ParsedIntent + sub-tasks
├── memory.py                       # AgentMemory + durable state/ persistence
├── decision.py                     # plan next step / pick next tool
├── action.py                       # MCP stdio client + tool dispatch
│
├── state/                          # gitignored; durable across runs
│   ├── memory.json                 # facts the agent has learned
│   └── runs/<timestamp>.jsonl      # per-run audit trail (optional)
├── sandbox/                        # gitignored; created by mcp_server.py file tools
│
├── queries/                        # the 4 target queries + expected outputs
│   ├── query_A.md
│   ├── query_B.md
│   ├── query_C.md
│   └── query_D.md
│
├── mcp-server/                     # LLM Gateway V3 lives here (already moved)
│   └── llm_gatewayV3/              # FastAPI service on port 8101
│       ├── main.py / router.py / providers.py / db.py / cache.py
│       ├── client.py               # Python SDK — every layer imports `from client import LLM`
│       ├── schemas.py              # gateway's own schemas (separate from agent's)
│       ├── run.sh                  # ./run.sh starts the gateway
│       └── ...
│
├── mcp-server-meeting-intel/       # PARKED: old Session-6 (meeting agent + MCP)
│   └── ...                         # untouched, no longer the canonical server
└── extension-meeting-intel/        # PARKED: old Chrome extension UI
    └── ...
```

Why this layout:

- `mcp_server.py` at the root, runnable as `uv run python mcp_server.py` — matches the assignment's stated invocation.
- Four cognitive layers as four separate files — required by the assignment, also keeps the contracts visible.
- `schemas.py` at the root holds **only the agent's** Pydantic contracts — the gateway has its own `schemas.py` (different concern: provider request/response, not cognitive-layer messages).
- `state/` and `sandbox/` are gitignored so attempts can be cleaned between runs.

---

## 2. The four cognitive layers — what each one does

```
                              User query
                                  │
                                  ▼
                            ┌──────────────┐
                            │  Perception  │   ParsedIntent (typed)
                            └──────┬───────┘
                                   │
                                   ▼
                            ┌──────────────┐
                            │   Memory     │   MemoryView (typed)
                            └──────┬───────┘   recall facts, prior plans
                                   │
                                   ▼
                            ┌──────────────┐
                            │   Decision   │   PlanStep (typed)
                            └──────┬───────┘   pick next tool/sub-task
                                   │
                                   ▼
                            ┌──────────────┐
                            │   Action     │   ActionResult (typed)
                            └──────┬───────┘   call MCP tool, return result
                                   │
                                   └─── loop back to Memory.update() ───┐
                                                                        │
                                                       agent6.py drives this loop
                                                       until Decision emits a
                                                       PlanStep with `done=True`
```

### Perception (`perception.py`)

- **Input**: raw user query string + a small MemoryView (what we already know).
- **Output**: `ParsedIntent`
  - `goal: str` — restated in agent's own words
  - `entities: list[Entity]` — typed: city, currency_pair, url, timezone, file_path, etc.
  - `expected_output_shape: str` — short description for the verifier later
- **LLM call**: gateway with `auto_route="perception"` (cheap classifier-tier).
- **Constraint**: structured output via `response_format` so we get a `ParsedIntent` straight from the model — no regex.

### Memory (`memory.py`)

- **Two responsibilities**:
  1. **Durable storage** in `state/memory.json` — facts that survive across runs (atomic write with a temp + rename pattern so a crash mid-write doesn't corrupt the file).
  2. **Recall** — given a `ParsedIntent`, surface facts relevant to it.
- **Output**: `MemoryView` with `facts: list[Fact]` and `prior_runs: list[RunSummary]`.
- **LLM call**: only if the recall step decides to summarize a long fact set — `auto_route="memory"`.
- **Critical for Query C** (durable memory): run 1's `Memory.write_fact()` must persist; run 2's `Memory.recall()` must read it.

### Decision (`decision.py`)

- **Input**: `ParsedIntent` + `MemoryView` + recent `ActionResult`s.
- **Output**: `PlanStep`
  - `done: bool`
  - `final_answer: str | None` (set when `done=True`)
  - `next_tool_call: ToolCall | None` (set when `done=False`)
- **LLM call**: gateway with `auto_route="decision"`. Tools list is passed to the LLM so it can emit a `tool_use`-style structured choice.
- **Constraint**: response is validated as `PlanStep`; the loop refuses to dispatch anything that isn't a typed tool call.

### Action (`action.py`)

- **Stdio MCP client** — spawns `python mcp_server.py` as a subprocess, talks JSON-RPC over stdin/stdout (the MCP Python SDK's stdio client transport).
- **Input**: `ToolCall` (name + args).
- **Output**: `ActionResult` (status, payload, error).
- **One retry on error**, then surface the error to the model.
- **No LLM call** — purely transport.

---

## 3. Pydantic contracts (`schemas.py`) — provided by user, load-bearing

You've supplied the canonical contracts. These are the only shapes that cross layer boundaries:

```python
class MemoryItem(BaseModel):
    id: str
    kind: Literal["fact", "preference", "tool_outcome", "scratchpad"]
    keywords: list[str]
    descriptor: str            # one short human-readable line
    value: dict                # structured payload
    artifact_id: str | None    # handle into the artifact store
    source: str
    run_id: str
    goal_id: str | None
    confidence: float
    created_at: datetime


class Artifact(BaseModel):
    id: str                    # "art:<sha256-prefix>"
    content_type: str
    size_bytes: int
    source: str
    descriptor: str


class Goal(BaseModel):
    id: str
    text: str                  # short imperative description
    done: bool
    attach_artifact_id: str | None


class Observation(BaseModel):  # perception output
    goals: list[Goal]


class ToolCall(BaseModel):
    name: str
    arguments: dict


class DecisionOutput(BaseModel):  # decision output — answer XOR tool_call
    answer: str | None
    tool_call: ToolCall | None
```

### What these shapes imply about the architecture

1. **Goal-driven, not query-driven.** Perception decomposes the user query into a list of `Goal`s (e.g. *"record favorite city = Vienna"* + *"fetch current weather for Vienna"* + *"answer the user"*). Each goal is a unit of work that the loop can mark `done=True` independently. This is more granular than the single `ParsedIntent` I'd sketched.

2. **Artifacts are first-class.** Anything large (a fetched web page's markdown, a file's contents, a multi-thousand-token tool result) is content-addressed and stored once on disk; everything else references it by `artifact_id`. This keeps memory items and LLM contexts small, and lets `state/` survive disk pressure better. Implementation:
   - On-disk layout: `state/artifacts/<sha256-prefix>.bin` for the bytes, `state/artifacts/<sha256-prefix>.json` for the `Artifact` metadata record.
   - `id = "art:<first 12 hex chars of sha256>"` — short but collision-safe at this scale.
   - Action layer writes artifacts when a tool returns large output (e.g. `fetch_url`); Memory and Decision layers reference them via `artifact_id`.

3. **MemoryItem.kind is the retention policy lever.**
   - `fact` — durable, indexed; survives runs.
   - `preference` — durable, indexed; survives runs. (Query C lives here.)
   - `tool_outcome` — write-through cache for expensive tool calls; durable but freshness-bounded.
   - `scratchpad` — per-run working memory; can be GC'd at run end.

4. **DecisionOutput is XOR.** Either `answer` (we're done with the current goal) or `tool_call` (we need one more tool to make progress). The validator that enforces this lives in `schemas.py`:
   ```python
   @model_validator(mode="after")
   def either_answer_or_tool(self):
       a, t = self.answer is not None, self.tool_call is not None
       if a == t:  # both or neither
           raise ValueError("DecisionOutput requires exactly one of answer | tool_call")
       return self
   ```

5. **There's no `ParsedIntent` and no `PlanStep`.** Those were my draft names; the user's `Observation` and `DecisionOutput` replace them. PLAN.md sections 4–6 are updated accordingly below.

### Supporting shapes I'll need to add to schemas.py

The user's six models are the inter-layer contracts. Three more are needed for internal bookkeeping (and are not exchanged between layers, but live in schemas.py for consistency):

```python
class ActionResult(BaseModel):
    tool: str
    arguments: dict
    status: Literal["ok", "error"]
    result: Any | None                    # JSON-able tool payload OR an artifact_id ref
    artifact_id: str | None               # set when result was offloaded to artifact store
    error: str | None
    retried: bool
    duration_ms: int

class RunRecord(BaseModel):                # one per /agents/run invocation
    run_id: str
    started_at: datetime
    ended_at: datetime | None
    user_query: str
    observation: Observation | None
    iterations: int
    final_answer: str | None
    status: Literal["running", "ok", "failed", "iteration_cap"]

class TraceEvent(BaseModel):              # written to state/runs/<run_id>.jsonl
    timestamp: datetime
    layer: Literal["perception","memory","decision","action","final"]
    goal_id: str | None
    payload: dict                          # model_dump of the layer's output
```

These three live in schemas.py so they can be imported by every layer + the tracer without circular imports. They are not part of the LLM-facing wire format.

---

## 4. The agent loop (`agent6.py`)

Goal-driven: perception emits an `Observation` with one or more `Goal`s. The loop processes goals in order. For each unfinished goal, the loop iterates Decision → Action until either Decision returns an `answer` (goal complete) or the iteration cap fires. Memory writes happen opportunistically inside the loop — when Action returns something worth remembering (a preference statement, an expensive tool result), Memory persists it.

```
def run(user_query: str) -> RunRecord:
    run_id = new_run_id()
    trace = Tracer(run_id)
    memory   = Memory.load()                              # reads state/memory.json
    artifacts = ArtifactStore()                           # state/artifacts/

    observation = Perception.observe(
        user_query, memory.recall_for_query(user_query)
    )
    trace.record("perception", observation)
    final_answer = None

    for goal in observation.goals:
        if goal.done:
            continue                                      # perception may pre-mark trivially-done goals
        history: list[ActionResult] = []
        for it in range(MAX_ITERATIONS_PER_GOAL):
            mem_view = memory.recall_for_goal(goal)
            decision = Decision.next(
                goal=goal,
                observation=observation,
                memory=mem_view,
                history=history,
            )
            trace.record("decision", decision, goal_id=goal.id)

            if decision.answer is not None:
                goal.done = True
                if is_final_goal(goal, observation):
                    final_answer = decision.answer
                # Opportunistic memory write — preferences declared in this run
                memory.maybe_persist_from_decision(goal, decision, run_id=run_id)
                break

            # decision.tool_call is non-None here (XOR enforced by the schema)
            result = Action.execute(decision.tool_call, artifacts=artifacts)
            trace.record("action", result, goal_id=goal.id)
            history.append(result)
            memory.maybe_cache_tool_outcome(goal, result, run_id=run_id)
        else:
            # iteration cap hit before goal closed
            trace.record("final", {"status": "iteration_cap", "goal": goal.id})
            raise IterationCapExceeded(goal.id)

    trace.record("final", {"answer": final_answer})
    return RunRecord(
        run_id=run_id, user_query=user_query, observation=observation,
        iterations=trace.iteration_count(), final_answer=final_answer,
        status="ok",
    )
```

- **MAX_ITERATIONS_PER_GOAL** = 2× the expected iteration count for the hardest query (set once the queries arrive).
- **Goal ordering**: perception's `goals` list is processed in order, but a later goal can read facts written by an earlier goal via `Memory.recall_for_goal()`.
- **The MCP subprocess** is spawned once at agent6.py startup and reused across iterations + goals — not respawned per tool call.
- **Run trace** is written to `state/runs/<run_id>.jsonl`, one line per `TraceEvent`. Used for the README's terminal-output sections.
- **The "final goal"** is whichever goal produces the user-facing answer — usually the last one in `observation.goals`, but Perception can mark a specific goal as the answer-bearer if needed (open question — see Section 9.E).

---

## 5. State / durable memory + artifact store

### Layout

```
state/
├── memory.json                # MemoryItem[] — durable across runs
├── runs/
│   ├── <run_id>.jsonl         # TraceEvent[] for that run, line-delimited
│   └── ...
└── artifacts/
    ├── <sha256-prefix>.bin    # raw bytes (or .txt / .md / .html for human-readable)
    └── <sha256-prefix>.json   # Artifact metadata record
```

### `state/memory.json` shape

```jsonc
{
  "version": 1,
  "items": [
    {
      "id": "mem:01HXZ...",
      "kind": "preference",
      "keywords": ["favorite_city"],
      "descriptor": "user's favorite city is Vienna",
      "value": {"slot": "favorite_city", "value": "Vienna"},
      "artifact_id": null,
      "source": "user_statement",
      "run_id": "run:01HXZ...",
      "goal_id": "goal:01HXZ...",
      "confidence": 0.95,
      "created_at": "2026-05-16T18:42:00Z"
    },
    ...
  ]
}
```

### Operational rules

- **Atomic writes**: write to `<path>.tmp` then `os.replace(...)`. Survives kill -9 mid-write.
- **Append-only is wrong here**: we may need to update a `MemoryItem`'s confidence or supersede a stale `preference`. So full-file rewrite, but atomic.
- **Artifact GC**: artifacts referenced by no live `MemoryItem` AND not referenced by any active run's trace can be GC'd. Out of scope for the first pass — write only, no GC. `rm -rf state/artifacts/` between attempts is the cleanup story.
- **Schema versioning**: top-level `"version": 1`. Bump and migrate at load if it ever changes.
- **Cleanability**: `rm -rf state/` between assignment attempts. The README documents this.

### Memory.recall semantics

- `Memory.recall_for_query(query: str) → list[MemoryItem]` — called once at perception time. Keyword-match against the raw user query.
- `Memory.recall_for_goal(goal: Goal) → list[MemoryItem]` — called every decision iteration. Keyword-match against `goal.text`.
- Retrieval is keyword-based for v1 (simple, deterministic, debuggable). Embeddings can come later if needed for the iteration budget.

### Query C trace (concrete)

| | Run 1 | Run 2 |
|---|---|---|
| User says | "Remember that my favorite city is Vienna" | "What's the weather in my favorite city?" |
| Perception emits | `Observation(goals=[Goal(text="store preference: favorite_city=Vienna")])` | `Observation(goals=[Goal(text="look up weather in user's favorite city"), Goal(text="answer the user")])` |
| Memory recall finds | (nothing relevant — empty) | `MemoryItem(kind="preference", value={"slot":"favorite_city","value":"Vienna"})` |
| Decision emits | `DecisionOutput(answer="Saved.")` after Memory.persist | `DecisionOutput(tool_call=web_search("weather Vienna"))`, then `DecisionOutput(answer="In Vienna it's …")` |
| `state/memory.json` after | contains the preference item | unchanged (read-only this run) |

---

## 6. LLM Gateway V3 integration

Every LLM call goes through the gateway. No direct provider SDKs in agent code.

```python
# Used by perception.py, memory.py, decision.py
from llm_gatewayV3.client import LLM
llm = LLM()  # http://localhost:8101 by default

# Perception — structured output with auto-routing
resp = llm.chat(
    prompt=PERCEPTION_PROMPT.format(query=q, memory=mv.model_dump_json()),
    response_format={"type":"json_schema","schema":ParsedIntent.model_json_schema()},
    auto_route="perception",
)
intent = ParsedIntent.model_validate(resp["parsed"])

# Decision — tool-using
resp = llm.chat(
    prompt=DECISION_PROMPT.format(intent=..., memory=..., history=...),
    response_format={"type":"json_schema","schema":PlanStep.model_json_schema()},
    auto_route="decision",
)
step = PlanStep.model_validate(resp["parsed"])
```

The router decides which worker tier (TINY / LARGE) handles each call. Perception is usually TINY; decision can be LARGE when the history is long; memory can be either.

**Startup dependency**: the gateway must be running on port 8101 before agent6.py starts. The README will document `./mcp-server/llm_gatewayV3/run.sh` as a prereq, and agent6.py will surface a clear error if the gateway is unreachable.

---

## 7. Validation prompts (deliverable: "Perception and Decision Prompt and Validation JSON of PoP")

The assignment asks for the Perception and Decision system prompts plus a sample of their structured-output JSON. These will live in:

- `perception.py` → `PERCEPTION_SYSTEM_PROMPT` constant
- `decision.py` → `DECISION_SYSTEM_PROMPT` constant
- `queries/query_*.md` → expected `ParsedIntent` and an example `PlanStep` for each query

The prompts will follow the same structured-reasoning rubric from Session 5 (Operating rules / Self-check rules / Reasoning transparency / Output format).

---

## 8. Task breakdown (concrete order of work)

1. **Park existing meeting-intel code** — `git mv mcp-server mcp-server-meeting-intel` and the extension files into `extension-meeting-intel/`. Update both READMEs to point at each other.
2. **Drop in `mcp_server.py`** at the repo root (the version you provided in the previous message — paste verbatim).
3. **Write `pyproject.toml` + `.python-version`** at the repo root with the new dep set: `mcp`, `pydantic>=2`, `httpx`, `ddgs`, `tavily-python`, `crawl4ai`, `python-dotenv`. Run `uv sync`.
4. **Write `.env.example`** with `TAVILY_API_KEY=` (optional) and `LLM_GATEWAY_V3_URL=http://localhost:8101`.
5. **Smoke-test `mcp_server.py` standalone** — spawn it, run `web_search`, `get_time`, `read_file` via stdio JSON-RPC. Confirm the 9-tool schema.
6. **Smoke-test the gateway** — start `./mcp-server/llm_gatewayV3/run.sh`, hit `GET /v1/routers`, then a tiny `POST /v1/chat` with `auto_route="perception"`.
7. **Write `schemas.py`** — all the Pydantic v2 models above.
8. **Write `action.py`** — stdio MCP client (subprocess + the MCP Python SDK's `stdio_client`). Exposes `Action.execute(ToolCall) → ActionResult`.
9. **Write `memory.py`** — `Memory.load()`, `Memory.view()`, `Memory.recall(intent)`, `Memory.write_fact(fact)`, `Memory.write_run_summary(...)`. Atomic writes.
10. **Write `perception.py`** — `Perception.parse(query, memory_view) → ParsedIntent`. Includes the system prompt and a deterministic post-validation step.
11. **Write `decision.py`** — `Decision.next(intent, memory, history) → PlanStep`. Includes the decision prompt + the 9-tool list.
12. **Write `agent6.py`** — the loop above. Subprocess management for the MCP server, gateway-readiness check, tracer.
13. **Iterate on the 4 queries** — adjust prompts and contracts until each converges within 2× the expected iteration count. This is where the bulk of the work is.
14. **Capture terminal output** for each query and paste into the README.
15. **Record the YouTube demo.**
16. **Final commit + push.**

Steps 1–6 are mechanical. Step 7 establishes the contracts. Steps 8–12 build the layers. Step 13 is where the real engineering happens — prompt-tuning to hit the iteration targets.

---

## 9. What I need from you before I start writing the cognitive layers

**B/C/D answered, A still pending.** The mechanical setup (steps 1–4 of section 8) doesn't need the queries and is being done right now. Steps 5+ wait.

### A. The 4 target queries themselves — **still blocking**

You said you'll paste them in your next message. Need:
- The exact text of each query (A / B / C / D).
- The expected final answer for each.
- The expected iteration count (so I know what 2× of "passing" means).
- Confirmation that **Query C** is the durable-memory one (run 1 writes, run 2 reads).

### E. New question raised by the goal-driven flow — which goal carries the final answer?

With `Observation.goals: list[Goal]`, perception may emit 1 or N goals. We need a deterministic rule for which goal's `DecisionOutput.answer` becomes the user-facing final answer when the run completes. Options:

| Rule | Pro | Con |
|---|---|---|
| **Last goal in the list wins** | Simple; matches the natural reading order ("first do X, then do Y, then answer the user") | Implicit |
| **Perception marks one goal as `is_final: bool`** | Explicit; survives reordering | Requires extending the `Goal` schema you supplied (adding `is_final`) |
| **First goal whose decision returns `answer != None` AND has no successor goals** | No schema change | Hard to reason about for multi-goal queries |

Recommendation: **Last goal wins** for v1 — it's the simplest rule that doesn't require schema changes. If a query needs different ordering, perception can reorder its goals list.

### F. Goal lifecycle — can a goal be re-opened?

Once `goal.done = True`, do we ever re-process it? Probably no — but a sub-goal may write a fact that invalidates a sibling goal's earlier decision. Recommendation: **no re-opening for v1**. If perception emits a wrong decomposition we surface it as an iteration-cap failure on a later goal.

### G. Confidence values on MemoryItems — how do we pick them?

`MemoryItem.confidence: float`. When user-declared (`"my favorite city is X"`) → 0.95. When agent-inferred from a tool result → 0.7. When transient scratchpad → 1.0 (no doubt, just ephemeral). Final policy can be tuned but I'll start with these defaults.

---

## Sign-off

Section 1 (parking) and Sections 8.1–8.4 (mechanical setup) are being executed now. Once the queries arrive I'll start Section 8.5+. The `schemas.py` file will be a direct transcription of your provided contracts (plus the three supporting shapes in §3) — no surprises.
