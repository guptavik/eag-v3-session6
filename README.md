# Meeting Intelligence Agent — Session 6

> **Session 6 upgrade — multi-agent system, fully Python.** The single-agent JavaScript loop became an **orchestrator + 2 specialist sub-agents** architecture, then the whole agent layer was moved out of the Chrome extension into the Python service. The extension is now a thin UI client that POSTs a query and reads back the reasoning chain via Server-Sent Events; all Gemini calls, memory, and tool dispatch live in `mcp-server/agents/`.
>
> ```
>                              ┌─────────┐    ┌───────────┐
>                              │ Memory  │    │  Tools    │
>                              │ (main)  │    │ delegate, │
>                              │         │    │ stats     │
>                              └────┬────┘    └─────┬─────┘
>                                   │               │
>                          ┌────────┴───────────────┴────────┐
>     User ◄── popup ──►   │      Orchestrator (main)        │   ┐
>             │ SSE        │  plans → delegates → synthesizes │   │
>             │            └────────┬────────────────────┬───┘   │  Python:
>             │                     │                    │       │  mcp-server/
>             │       ┌─────────────▼──────┐  ┌──────────▼──────┐│  agents/
>             │       │ Workspace sub-agent│  │ Research sub-ag.││
>             │       │  Memory + Tools    │  │  Memory + Tools ││
>             │       │  - getUpcoming…    │  │  - analyzeAtt…  ││
>             │       │  - searchGmail     │  │  - searchWebInfo││
>             │       └────────────────────┘  └─────────────────┘┘
>             │
>      Chrome extension (popup.html + agent-client.js)
> ```
>
> The orchestrator never calls calendar/email/web tools directly. It calls `delegate({agent, task})`, an in-process synthetic tool that routes to the named sub-agent's `run()`. The sub-agent runs its own short Gemini loop with only its scoped tools, then returns a structured summary the orchestrator folds into the final brief. Memory is per-agent and persists for the lifetime of the server process.
>
> Source layout — each agent gets its own folder under `mcp-server/agents/` with `prompt.py` + `tools.py`:
>
> ```
> mcp-server/agents/
>   memory.py             AgentMemory: bounded {task → summary} history + facts
>   sub_agent.py          Generic SubAgent class (own LLM loop, own memory)
>   llm.py                Multi-turn Gemini wrapper + JSON-Schema → Gemini sanitizer
>   registry.py           Partitions FastMCP's tool list into the per-agent subsets,
>                         exposes the run() entry point used by the HTTP layer
>   runner.py             Async generator that streams events as SSE
>   main/
>     prompt.py           ORCHESTRATOR_SYSTEM_PROMPT
>     tools.py            DELEGATE_TOOL schema + MAIN_DIRECT_TOOL_NAMES
>   workspace/
>     prompt.py           WORKSPACE_SYSTEM_PROMPT
>     tools.py            WORKSPACE_TOOL_NAMES = {getUpcomingMeetings, searchGmail}
>   research/
>     prompt.py           RESEARCH_SYSTEM_PROMPT
>     tools.py            RESEARCH_TOOL_NAMES = {analyzeAttendeeBackground, searchWebInfo}
> ```
>
> To add a tool to a sub-agent: edit just `agents/{name}/tools.py`. To re-tune a prompt: edit just `agents/{name}/prompt.py`. The Gemini API key is now a single server-side .env variable (no per-user storage in the extension).

A Chrome extension that prepares you for upcoming meetings by autonomously gathering context — calendar, email, attendee profiles, company info — and synthesizing it into an actionable brief. Built on Google Gemini 2.5 Flash, organized as a multi-agent system (orchestrator + 2 specialist sub-agents). Tools live in a local **Python 3.12 + Pydantic v2** MCP server that hits real APIs (Google Calendar, Gmail, SerpAPI, Gemini for synthesis).

> **Carried over from Session 5:** structured-reasoning prompt with `[LOOKUP]` / `[SYNTHESIS]` / `[SCHEDULING]` / `[SEARCH]` / `[PROFILE]` tags, three self-check gates, fallback rules, inline-confidence annotations, and a `⚠️ Missing Context` section in the brief for unrecoverable gaps. These rules now live in the orchestrator's system prompt; sub-agents follow tighter scope-specific prompts that defer brief-writing to the orchestrator. The original evaluator scoring is in **[docs/prompt-evaluation.md](docs/prompt-evaluation.md)**.
>
> 📺 **Demo video (single-agent baseline):** https://youtu.be/fBIHwz54rvE

## Prompt qualification (carried over from Session 5)

| Evaluator criterion | Session 4 | Session 5 |
|---|:---:|:---:|
| Explicit reasoning instructions | ❌ | ✅ |
| Structured output format | ✅ | ✅ |
| Separation of reasoning and tools | ❌ | ✅ |
| Conversation loop support | ✅ | ✅ |
| Instructional framing (examples) | ✅ | ✅ |
| Internal self-checks | ❌ | ✅ |
| Reasoning-type awareness | ❌ | ✅ |
| Error handling / fallbacks | ✅ | ✅ |
| **Total true** | **4 / 8** | **8 / 8** |

The full evaluator JSON output for both prompts, plus a per-criterion mapping to specific sections of the new `SYSTEM_PROMPT`, is in **[docs/prompt-evaluation.md](docs/prompt-evaluation.md)**.

### Latest evaluator pass — post-bugfix review

After the all-day-events fix landed and the corresponding `excludedAllDay` instruction was added to the prompt, the prompt was re-scored. The fix that prompted the re-review: replacing the hardcoded `2` in the example with the `{{excludedAllDay}}` placeholder so the model interpolates the real tool value instead of echoing the literal number.

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
  "overall_clarity": "9/9 — clean and production-ready. The {{excludedAllDay}} dynamic variable fix from the previous review has been applied correctly. No new gaps found. This is the strongest version of the prompt reviewed so far."
}
```

**Change confirmed**

| Field | Before | After |
|---|---|---|
| `excludedAllDay` example | Hardcoded `2` — model might echo it literally | `{{excludedAllDay}}` — model interpolates the real tool value |

**Nothing left to flag.** The prompt now covers all nine dimensions cleanly:

- Reasoning is planned and tagged before tool calls
- Output format is rigid and parseable
- Tool phases are sequenced and gated by self-checks
- Gaps surface visibly (`⚠️ Missing Context`, inline confidence notes) rather than silently dropping
- Fallback behavior is defined for every failure mode

At this point, the next improvements would come from **real usage data** — e.g. edge cases where the model picks the wrong `hoursAhead` value, or misclassifies an internal attendee as external — rather than anything addressable in the prompt text itself.

## What it does

Ask it questions like:

- *Prepare me for my next meeting*
- *Show me all meetings today and research the attendees*
- *What's my meeting load this week?*

It plans, calls 3–7 tools (calendar, email, web/LinkedIn, attendee profiles, stats), and returns a structured markdown brief with attendee cards, talking points, and a prep checklist. Every plan line is tagged with the kind of reasoning it represents (`[LOOKUP]` / `[SCHEDULING]` / `[SEARCH]` / `[PROFILE]` / `[SYNTHESIS]`) — visible in the reasoning chain in the popup.

## Architecture

```
┌─ Chrome Extension (MV3) ────────────────────┐    ┌─ External services ─────┐
│                                             │    │                         │
│  popup.html / popup.js / styles.css ← UI    │    │  Gemini API (3 agents)  │
│         │                                   │    │  Google Calendar API    │
│  agent-client.js                            │    │  Gmail API              │
│   POST /agents/run + SSE reader             │    │  SerpAPI (Google SERP)  │
│         │                                   │    │                         │
└─────────┼───────────────────────────────────┘    └────▲────▲────▲──────────┘
          │ POST /agents/run :3737                      │    │    │
          │ stream SSE back                             │    │    │
          ▼                                             │    │    │
┌─ Local Python service (mcp-server/) ───────────────┐ │    │    │
│ Python 3.12 + Pydantic v2, managed by uv           │ │    │    │
│                                                    │ │    │    │
│  ┌─ Multi-agent runtime ───────────────────────┐  │ │    │    │
│  │  agents/runner.py    SSE event emitter      │  │ │    │    │
│  │  agents/registry.py  partitions tools,      │  │ │    │    │
│  │                      builds the registry    │  │ │    │    │
│  │  agents/sub_agent.py SubAgent (LLM loop) ───┼──┼─┘    │    │
│  │  agents/memory.py    AgentMemory            │  │      │    │
│  │  agents/llm.py       Gemini multi-turn ─────┼──┘      │    │
│  │  agents/{main,workspace,research}/          │  │      │    │
│  │    prompt.py + tools.py                     │  │      │    │
│  └─────────────────────────────────────────────┘  │      │    │
│                                                    │      │    │
│  ┌─ MCP transport (unchanged) ─────────────────┐  │      │    │
│  │  server.py  FastMCP + Starlette on /mcp     │  │      │    │
│  │  tools.py   5 async tool implementations    │  │      │    │
│  │  models.py  Pydantic v2 I/O models          │  │      │    │
│  │   ├ getUpcomingMeetings  ──→ Calendar ──────┼──┼──────┘    │
│  │   ├ searchGmail          ──→ Gmail ─────────┼──┼───────────┘
│  │   ├ searchWebInfo        ──→ Gemini→SerpAPI │  │
│  │   ├ analyzeAttendeeBackground ─→ SerpAPI+Gemini │
│  │   └ calculateMeetingStats ─→ pure compute   │  │
│  │  google_auth.py · serpapi.py · llm.py · cache.py
│  │                                             │  │
│  │  Agents call tools via mcp.call_tool() — same
│  │  validation + handlers as the MCP transport │  │
│  └─────────────────────────────────────────────┘  │
│                                                    │
│  ~/.meeting-intel-mcp/google-tokens.json (OAuth)   │
│  mcp-server/.env  (GEMINI_API_KEY, SERPAPI_API_KEY, etc.)
└────────────────────────────────────────────────────┘
```

The extension is a thin client: the popup POSTs the user's query to `/agents/run` and renders the streamed SSE events. All Gemini calls, agent memory, and tool dispatch live in Python. The orchestrator never calls calendar/email/web tools directly — it calls `delegate({agent, task})`, which is resolved in-process by routing the task to the named sub-agent's `run()`. Each sub-agent runs its own short Gemini loop with only its scoped tools and its own `AgentMemory`, then returns a structured summary the orchestrator folds into the final brief.

Either side can still be swapped: the MCP layer (`/mcp` route + `tools.py`) is unchanged from Session 5, so a different MCP host (Claude Desktop, Cursor) can use the same five tools without the agent runtime; conversely, a different UI (CLI, web page) can call `/agents/run` without touching the MCP layer.

### Agent flow

Worked example for *"Prepare me for my next meeting"*. The orchestrator now does two delegation rounds and one synthesis pass — typically 3 orchestrator turns, with each delegation triggering 1–3 sub-agent turns under the hood.

```
EXTENSION    → POST /agents/run {query: "Prepare me for my next meeting", userTimeZone: "America/Chicago"}
                ▼
SSE stream begins. Each event ← single line of work; extension renders it live.

ORCHESTRATOR turn 1 → tool_use: delegate({agent: "workspace",
                                          task: "fetch upcoming meetings, next 24h"})
    │   emits step(workspace, getUpcomingMeetings, loading) … success
    └─► WORKSPACE sub-agent
          turn 1: getUpcomingMeetings({hoursAhead: 24})
                  ── in-process mcp.call_tool ──→ tools.py ──→ Google Calendar
          turn 2: end_turn — returns structured summary
                  ("Found 1 meeting: Acme Q4 Sync, Tue 2pm, attendees …")

ORCHESTRATOR turn 2 → tool_use blocks (sequential within one turn):
    │   delegate({agent: "workspace", task: "search email for 'Acme'"})
    │   delegate({agent: "research",  task: "profile attendees X,Y + Acme Corp"})
    │
    ├─► WORKSPACE sub-agent
    │     turn 1: searchGmail({query: "Acme"}) ── in-process ──→ Gmail
    │     turn 2: end_turn — returns 3 hit summary
    │
    └─► RESEARCH sub-agent
          turn 1: parallel analyzeAttendeeBackground × 2 + searchWebInfo
                  ── in-process ──→ SerpAPI + Gemini
          turn 2: end_turn — returns profile + company summary

ORCHESTRATOR turn 3 → end_turn: final markdown brief
                       SSE emits: final_text → done
                       popup.js renders attendees, talking points, prep checklist
```

### Key architecture points

1. **Three agents, one Python service.** Orchestrator (`agents/main/`) + workspace (`agents/workspace/`) + research (`agents/research/`), all built on the same `SubAgent` base class in `agents/sub_agent.py`. Each agent owns its own system prompt, its own Gemini loop, and its own `AgentMemory`. The orchestrator's `delegate` tool is the only data-acquisition path it has — it cannot call MCP tools directly.
2. **Memory is per-agent and survives across requests in the same server process.** Each sub-agent's recent `(task → summary)` history is prepended to its next user message, so a follow-up question reuses prior context without re-fetching. Capped at 5 entries per agent; cleared on server restart.
3. **Streaming via SSE.** The extension POSTs once and reads named events as they land: `step`, `assistant_text`, `final_text`, `done`, `error`. No polling, no WebSockets.
4. **Conversation history is stateless inside each agent's loop** — every Gemini call includes the full prior turns + tool results. The orchestrator only ever sees the sub-agent's *final summary*, not the sub-agent's interior dialogue, which keeps its context window small.
5. **Iterative refinement** — each agent continues until Gemini stops emitting tool_use blocks. Orchestrator cap: 10 iterations; sub-agent cap: 6.
6. **Per-step retry** — failed tool calls retry once silently; persistent failures land in the conversation as `is_error: true` so the agent can adapt.
7. **In-process tool execution.** Sub-agents resolve tool calls by invoking `mcp.call_tool()` directly — same Pydantic validation, same handlers as the MCP wire transport, no HTTP hop. The user's timezone is propagated via a `contextvars.ContextVar` set at the start of each `/agents/run` request.
8. **Visible reasoning, agent-tagged.** Every tool call (from any agent) streams as a separate `step` event. Each row in the UI carries a colored pill — `main` / `workspace` / `research` — and a matching left-border accent so the user can see at a glance which agent did which work.
9. **MCP transport is unchanged.** `/mcp` still speaks streamable-HTTP MCP; another MCP-aware client (Claude Desktop, Cursor) could use the same five tools without any agent runtime.

## Tools

Five tools, exposed by the MCP server via JSON Schema. The extension fetches the schema list at popup open via MCP `tools/list`:

| Name | Backend | Notes |
|---|---|---|
| `getUpcomingMeetings` | **Google Calendar** | reads `primary` calendar, recurrences expanded, cancelled events dropped, user themselves removed from attendees, original `dateTime` offset preserved + `timeZone` field exposed. Supports `endOfToday: true` to bound the fetch to the end of the current calendar day in the user's timezone (prevents tomorrow's meetings from appearing in "today" queries) |
| `searchGmail` | **Gmail** | accepts native Gmail query syntax; returns subject / from / date / snippet for up to 20 hits |
| `searchWebInfo` | **Gemini → SerpAPI** | tiered (see below) |
| `analyzeAttendeeBackground` | **SerpAPI + Gemini** | tiered; **0 API calls** for `OWN_COMPANY_DOMAIN` attendees |
| `calculateMeetingStats` | real computation | accepts `hoursAhead` (preferred — fetches its own meetings via Calendar) or an explicit `meetings` array; returns counts, hours-per-day, busiest day, per-day load classification, per-day meeting list. All-day and multi-day events (≥ 24 h) are excluded from hour totals so they don't inflate the load numbers |

### SerpAPI / Gemini tiering

SerpAPI's free tier is 100 searches/month, so the server uses Gemini wherever Gemini is competent and only spends SerpAPI quota where it actually helps.

**`searchWebInfo`:**
- Query mentions *news / recent / latest / today / current / funding / 2026+ / etc.* → **SerpAPI directly** (Gemini's knowledge cutoff makes it useless for fresh data).
- Otherwise → **Gemini first**. If it knows the entity, return its structured answer. If it returns `_unknown` or the call fails, **fall back to SerpAPI**.

**`analyzeAttendeeBackground`:**
- Email domain in `OWN_COMPANY_DOMAIN` → **0 API calls**, return an "internal teammate" stub.
- Otherwise → **1 SerpAPI call** (the only reliable source for the LinkedIn URL — Gemini hallucinates URLs) + **1 Gemini call** to synthesize `currentRole` and `background` from the SerpAPI snippets.

A process-local LRU (`mcp-server/cache.py`, 50 entries) with in-flight dedupe handles repeat lookups within a popup session.

## Agent loop (each agent)

Every agent — orchestrator and both sub-agents — runs the same generic loop, implemented once in [mcp-server/agents/sub_agent.py](mcp-server/agents/sub_agent.py) and instantiated three times with different system prompts and tool subsets.

```
SubAgent.run(task, emit, user_time_zone)
    │
    ▼
prepend memory.serialize() to task
    │
    ▼
loop (max iterations per agent):
    call_llm(history, this.tools, system_prompt, user_time_zone)
       │
       ▼
    if stop_reason != "tool_use": return final text  → memory.record_call()
    for each tool_use block (sequential):
        if tool_handlers[name] (orchestrator's "delegate") → in-process
        else → mcp.call_tool(name, args) — Pydantic validates + tools.py executes
        retry once on error
        push tool_result (is_error: true on persistent failure)
        await emit({kind: "step", ...})    → flows out as SSE
    push assistant turn + tool_results into history
```

- **Caps:** orchestrator = 10 iterations, sub-agents = 6. Sub-agents are scoped so they finish in 1–3 turns.
- **Multiple `tool_use` blocks** in one assistant turn execute **sequentially** so the reasoning-chain UI orders them deterministically. Multiple `delegate` calls in one orchestrator turn fan out to sub-agents the same way.
- **Tool failures** retry once silently; persistent failures surface as `is_error: true` tool results so the agent can adapt.
- **User timezone** is detected in the browser via `Intl.DateTimeFormat()`, posted in the `/agents/run` body, and scoped for the request via a `contextvars.ContextVar` so every `mcp.call_tool()` auto-injects it just like the old MCP client did on the wire.
- **Memory is per-agent and process-scoped.** Each `SubAgent` instance lives at module scope in [agents/registry.py](mcp-server/agents/registry.py), so its `AgentMemory` (capped at 5 prior `(task → summary)` entries) survives across `/agents/run` requests within the same server process.

## System prompts (three of them)

The single SYSTEM_PROMPT from Session 5 was split into three scoped prompts, one per agent folder:

1. **[`ORCHESTRATOR_SYSTEM_PROMPT`](mcp-server/agents/main/prompt.py)** — drives the main agent. Lists the two sub-agents and the one direct tool (`calculateMeetingStats`), describes when to delegate vs. compute, carries forward the Session 5 self-check gates and reasoning-transparency tags, and owns the final brief format (hero meta, Attendees, Company Context, Related Emails, Talking Points, Prep Checklist).
2. **[`WORKSPACE_SYSTEM_PROMPT`](mcp-server/agents/workspace/prompt.py)** — drives the workspace sub-agent. Scoped to Google Workspace tools only (`getUpcomingMeetings`, `searchGmail`). Tells the model to stay in its lane, return a compact structured summary (not a brief), and use parallel calls when independent. Knows `endOfToday: true` vs `hoursAhead` for "today" queries.
3. **[`RESEARCH_SYSTEM_PROMPT`](mcp-server/agents/research/prompt.py)** — drives the research sub-agent. Scoped to external research tools only (`analyzeAttendeeBackground`, `searchWebInfo`). Tells the model to parallelize independent lookups, skip internal-domain attendees with a stub, and surface "no result" gaps rather than fabricate.

All three share the four-section structure from Session 5: Operating rules, Self-check rules, Reasoning transparency rules, Final response format — but each is scoped to that agent's specific job. Per-call, every prompt has the user's local timezone appended so meeting times render in the user's zone (e.g. `2:00 PM CST`).

The full evaluator output (before / after) and per-criterion mapping is in **[docs/prompt-evaluation.md](docs/prompt-evaluation.md)**.

### How structured reasoning shows up in the UI

The agent's plan lines now embed `[TAG]` markers. [popup.js](popup.js) `splitTaggedBlocks()` recognizes the tags and renders each block as a distinct colored row with an icon + tag pill — so the reasoning chain is a structured sequence, not a single italicized stream:

| Tag | Icon | Color | What it means |
|---|---|---|---|
| `[LOOKUP]` | 🔎 | purple | retrieval — calendar / email / web / attendee |
| `[SYNTHESIS]` | ✍️ | red | composing the brief from gathered facts |
| `[SCHEDULING]` | 📅 | blue | calendar-window math / stats |
| `[SEARCH]` | 🌐 | amber | web / email search lookups |
| `[PROFILE]` | 👤 | green | attendee background lookups |

## UI

- **No API-key configuration.** The Gemini key now lives server-side in `mcp-server/.env`; the popup no longer prompts for it.
- **Quick action buttons** plus a custom query input.
- **Reasoning chain** — every tool call rendered as a collapsible row with status icon (loading, retrying, success, error). The whole chain is also collapsible. Reasoning prose between tool calls is rendered as a collapsed "thought" with a one-line preview.
- **Brief renderer** — the model's markdown output is post-processed into structured blocks:
  - Hero card with title, gradient header, When/Where/Agenda meta strip.
  - Attendee cards with initial-letter avatar circles.
  - Email cards with date pills.
  - Talking points as numbered cards.
  - Prep checklist as checkbox-styled rows.
- **Stats card** for `calculateMeetingStats` — 2x2 metric tile grid, hours-based weekly load chart with per-day color-coded bars, collapsible day-by-day breakdown.
- **Multi-meeting briefs** — each `# Meeting Title` becomes its own collapsible card; all collapsed by default.

## Setup

### 1. Install the Python service (MCP + agents)

The service is **Python 3.12 + Pydantic v2** managed by **[uv](https://docs.astral.sh/uv/)**. It hosts both the MCP tool transport (`/mcp`) and the multi-agent runtime (`/agents/run`).

```sh
# install uv once if you don't have it (macOS/Linux):
#   curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell):
#   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

cd mcp-server
uv sync                     # creates .venv/ and installs deps from pyproject.toml
cp .env.example .env
# fill in GEMINI_API_KEY (or GOOGLE_API_KEY)  ← required by the agents
#         SERPAPI_API_KEY                      ← required by searchWebInfo / attendee profiles
#         GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET  ← required by Calendar / Gmail
#         OWN_COMPANY_DOMAIN                   ← optional, marks internal attendees
uv run python server.py
```

See [mcp-server/README.md](mcp-server/README.md) for the full credential walkthrough (SerpAPI signup, Google Cloud OAuth client, etc.).

Endpoints:
- `http://localhost:3737/mcp` — MCP tool transport (streamable HTTP)
- `http://localhost:3737/agents/run` — multi-agent runtime, returns SSE
- `http://localhost:3737/health` — JSON health check

### 2. Install the Chrome extension

1. Open `chrome://extensions`, enable **Developer mode**, click **Load unpacked**, select the project root.
2. Click the extension icon in the toolbar.
3. Click any quick-action button or type a custom query.

No API-key configuration is needed in the popup — Gemini is called server-side, with `GEMINI_API_KEY` read from `mcp-server/.env`.

The first time `getUpcomingMeetings` or `searchGmail` runs, the Python server auto-opens a Google consent page in your browser. Authorize once → the refresh token persists at `~/.meeting-intel-mcp/google-tokens.json` → subsequent calls are silent.

## File structure

```
eag-v3-session6/
├── manifest.json             # MV3 config (host_permissions: localhost:3737 only)
├── popup.html                # UI layout — loads agent-client.js + popup.js
├── popup.js                  # UI controller, brief post-processor, markdown renderer,
│                             # splitTaggedBlocks() + per-agent step pill rendering
├── styles.css                # All styles, incl. per-agent (main/workspace/research)
│                             # pill + left-border accents and reasoning-block tints
├── agent-client.js           # POST /agents/run + SSE reader; calls UI callbacks
├── icons/                    # Extension icons
├── docs/
│   └── prompt-evaluation.md  # Prompt Evaluation Assistant scoring (Session 5)
├── README.md                 # This file
├── specification.md          # Original spec
└── mcp-server/               # Python 3.12 + Pydantic v2, managed by uv
    ├── pyproject.toml        # uv-managed dependency manifest
    ├── .python-version       # pinned to 3.12
    ├── server.py             # FastMCP + Starlette: /mcp · /agents/run · /health · OAuth
    ├── tools.py              # 5 async MCP tool implementations
    ├── models.py             # Pydantic v2 I/O models
    ├── google_auth.py        # OAuth client + ~/.meeting-intel-mcp/ token persistence
    ├── serpapi.py            # async SerpAPI client (httpx)
    ├── llm.py                # JSON-mode Gemini wrapper (server-side reasoning)
    ├── cache.py              # process-local LRU with in-flight dedupe
    ├── .env.example          # Required env-var template
    ├── README.md             # Server-specific setup walkthrough
    └── agents/               # ── Multi-agent runtime, one folder per agent ──
        ├── memory.py         # AgentMemory: bounded {task → summary} history + facts
        ├── sub_agent.py      # Generic SubAgent class — own LLM loop, own memory
        ├── llm.py            # Multi-turn Gemini wrapper + tool-schema sanitizer
        ├── registry.py       # Partitions FastMCP tool list, builds the agent registry,
        │                     # exposes run() — the public entry point
        ├── runner.py         # Async generator that streams events as SSE
        ├── main/              # Orchestrator
        │   ├── prompt.py     # ORCHESTRATOR_SYSTEM_PROMPT
        │   └── tools.py      # DELEGATE_TOOL schema + MAIN_DIRECT_TOOL_NAMES
        ├── workspace/         # Google Workspace specialist
        │   ├── prompt.py     # WORKSPACE_SYSTEM_PROMPT
        │   └── tools.py      # WORKSPACE_TOOL_NAMES = {getUpcomingMeetings, searchGmail}
        └── research/          # External-research specialist
            ├── prompt.py     # RESEARCH_SYSTEM_PROMPT
            └── tools.py      # RESEARCH_TOOL_NAMES = {analyzeAttendeeBackground, searchWebInfo}
```

## Tech stack

- **Extension** — plain HTML/CSS/JavaScript, no framework, no build step. Manifest V3. Two small JS files: `agent-client.js` (SSE reader) and `popup.js` (UI).
- **Python service** — Python 3.12, PEP 621 `[project]`, `mcp` (official Python SDK), `pydantic` v2, `starlette`, `uvicorn`, `httpx`, `google-api-python-client`, `google-auth-oauthlib`, `python-dotenv`. Managed by **[uv](https://docs.astral.sh/uv/)**; see [mcp-server/pyproject.toml](mcp-server/pyproject.toml). Hosts both the MCP tool transport and the multi-agent runtime.
- **LLM** — Gemini 2.5 Flash for the agent loop (extension) and for server-side reasoning (server). The same key works in both places.
- **External APIs** — Google Calendar, Gmail, SerpAPI (Google SERP).
- **Persistence** — `chrome.storage.local` (Gemini key for the agents), `~/.meeting-intel-mcp/google-tokens.json` (OAuth refresh token), `mcp-server/.env` (server-side API keys + config). Per-agent `AgentMemory` is in-process only and lives for the popup's lifetime.

## Limitations

- **Single user, single device.** API keys live in extension storage and `.env`; not multi-tenant safe. The OAuth client is per-user.
- **Local MCP server.** The extension talks to `localhost:3737`. Stop the server → tools fail with a clear "MCP server not running" error.
- **No conversation persistence.** Each popup session is independent; closing the popup loses history.
- **No streaming.** Each LLM turn is a buffered POST/response cycle.
- **Gemini knowledge cutoff** — `searchWebInfo`'s Gemini-first tier can be wrong for entities created/changed after the cutoff. Freshness keywords route to SerpAPI as a workaround, and Gemini's `_unknown` answer triggers SerpAPI fallback automatically.
- **SerpAPI free tier is 100 searches/month** — the tiering keeps usage low (most lookups go to Gemini), but heavy daily use will exhaust it. Upgrade to a paid SerpAPI plan or swap providers in [mcp-server/serpapi.py](mcp-server/serpapi.py).

## Future enhancements

### Short term

- **Real web search beyond Google SERP** — [mcp-server/serpapi.py](mcp-server/serpapi.py) is a thin adapter; swapping for Tavily, Brave, or a self-hosted SearXNG is a one-file change.
- **Recent-news enrichment** — populate `recentNews[]` for `searchWebInfo` companies via a SerpAPI news-engine call (currently empty by default).
- **Streaming responses** — switch to Server-Sent Events for the Gemini call so reasoning prose shows token-by-token within a turn.
- **Conversation persistence** — move the agent loop into a background service worker so long-running tasks survive popup close.

### Medium term

- **Action tools, not just read tools** — `draftEmailReply`, `proposeMeetingTime`, `addToCalendar`, `bookFollowUp` — so the agent can act, not just inform.
- **Cross-meeting context** — surface email threads or shared attendees that span multiple upcoming meetings.
- **Pre-warming** — background fetch the next meeting and pre-compute a brief on a schedule so the popup opens with the brief already prepared.
- **Settings beyond the API key** — model picker, iteration cap, default lookahead window, per-tool mock-vs-live toggle.
- **Caching beyond the LRU** — Gemini context caching for the system prompt + tool declarations (cheap once warmed).
- **Provider abstraction** — formalize the `api.js` translation layer into a pluggable adapter (Gemini / Anthropic / OpenAI) selectable from settings.

### Longer term

- **Drop-in to other MCP hosts** — the server already speaks streamable-HTTP MCP; pointing Claude Desktop or Cursor at `http://localhost:3737/mcp` should work out of the box.
- **Multi-turn refinement in-popup** — chat back and forth with the agent, not just one query → one brief.
- **Voice input** via Web Speech API.
- **Distribution via Chrome Web Store** with a hosted MCP server + proxied API keys so users don't have to bring their own.
- **Evals** — a fixed set of meeting scenarios + golden briefs, run on CI to catch regressions when the model, tools, or system prompt change.
