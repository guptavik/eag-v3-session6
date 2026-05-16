# EAG v3 — Session 6: Four-Layer Cognitive Agent

> **Status:** mechanical setup landed; cognitive layer code (`schemas.py`, `perception.py`, `memory.py`, `decision.py`, `action.py`, `agent6.py`) is **not yet written**. See [PLAN.md](PLAN.md) for the full implementation plan and the open question (the 4 target queries).

This repo hosts the Session 6 assignment: an agent built as **four cooperating cognitive layers** — perception → memory → decision → action — that uses the [LLM Gateway V3](mcp-server/llm_gatewayV3/) as its sole LLM substrate and talks to a stdio MCP server with 9 general-purpose tools. State is durable across runs in `state/`.

---

## Layout

```
.
├── PLAN.md                       implementation plan + open questions
├── mcp_server.py                 9-tool MCP server (stdio transport)
├── pyproject.toml                uv-managed; deps: mcp, pydantic, httpx,
│                                 ddgs, tavily-python, crawl4ai, python-dotenv
├── .env.example                  TAVILY_API_KEY (optional) + gateway URL
├── .python-version               3.12
├── .gitignore                    state/, sandbox/, usage.json, .venv, ...
│
├── (coming)  schemas.py          Pydantic v2 contracts for every layer
├── (coming)  perception.py       parse user query → Observation(goals=[...])
├── (coming)  memory.py           AgentMemory + state/ persistence + artifacts
├── (coming)  decision.py         pick next tool call or emit final answer
├── (coming)  action.py           MCP stdio client + tool dispatch
├── (coming)  agent6.py           the loop that wires the layers together
├── (coming)  queries/            the 4 target queries + expected outputs
│
├── mcp-server/
│   └── llm_gatewayV3/            FastAPI service on :8101 — every LLM call
│                                 routes through here (perception / memory /
│                                 decision use auto_route= for tier selection)
│
├── mcp-server-meeting-intel/     PARKED: prior Session-5/6 meeting-intel MCP
│                                 server (Python + Pydantic, /agents/run SSE)
└── extension-meeting-intel/      PARKED: prior Chrome extension UI
```

The two `*-meeting-intel/` folders preserve the prior Session 5 / 6 work and aren't part of the new assignment. They each still run independently — see `mcp-server-meeting-intel/README.md` for the calendar-agent setup.

---

## The 9 tools (in `mcp_server.py`)

| Tool | What it does |
|---|---|
| `web_search(query, max_results=5)` | Tavily primary, DuckDuckGo fallback. Hard-capped at 5 results. Usage logged to `./usage.json`, soft-capped at 950/1000 on Tavily. |
| `fetch_url(url)` | Headless-Chromium fetch via crawl4ai → clean markdown. |
| `get_time(timezone="UTC")` | Current time in a named IANA timezone. |
| `currency_convert(amount, from, to)` | ISO-3 conversion via frankfurter.dev. |
| `read_file(path)` | UTF-8 read from `./sandbox/`. |
| `list_dir(path=".")` | Directory listing under `./sandbox/`. |
| `create_file(path, content)` | Create new file in sandbox (errors if exists). |
| `update_file(path, content)` | Overwrite existing sandbox file. |
| `edit_file(path, find, replace, replace_all=False)` | Find-and-replace inside a sandbox file. |

All file ops are sandboxed under `./sandbox/` with path-escape protection. Path traversal raises `ValueError`.

---

## Architecture (planned — see PLAN.md for the full version)

```
User query
    │
    ▼
┌──────────────┐
│  Perception  │  → Observation(goals=[Goal, ...])
└──────┬───────┘
       ▼
┌──────────────┐
│   Memory     │  → MemoryItem[] (durable across runs in state/memory.json)
└──────┬───────┘     Artifacts in state/artifacts/<sha256>.{bin,json}
       ▼
┌──────────────┐
│   Decision   │  → DecisionOutput(answer | tool_call)   ← XOR
└──────┬───────┘
       ▼
┌──────────────┐
│   Action     │  → ActionResult  (stdio MCP call)
└──────┬───────┘
       │
       └── loop back via memory.maybe_persist_* ─── until DecisionOutput.answer
```

The Pydantic contracts (`MemoryItem`, `Artifact`, `Goal`, `Observation`, `ToolCall`, `DecisionOutput`) are typed end-to-end. No free-form dicts cross layer boundaries; no regex on LLM output (structured output via the gateway).

---

## Setup (mechanical — what's currently runnable)

### 1. Install Python deps

```sh
# Once, if you don't have uv:
#   curl -LsSf https://astral.sh/uv/install.sh | sh                  # macOS / Linux
#   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows

uv sync
```

This pulls `mcp`, `pydantic`, `httpx`, `ddgs`, `tavily-python`, `crawl4ai`, `python-dotenv`, and their transitive deps into `.venv/`.

### 2. Start the LLM Gateway

```sh
cd mcp-server/llm_gatewayV3
./run.sh                  # listens on http://localhost:8101
```

Set provider keys in `mcp-server/llm_gatewayV3/.env` (or `../.env` from its perspective — same file as the V1/V2 gateway versions). At minimum one of `GEMINI_API_KEY`, `GROQ_API_KEY`, `CEREBRAS_API_KEY`, etc. See `mcp-server/llm_gatewayV3/README.md`.

Health check:
```sh
curl -s http://localhost:8101/v1/routers | python -m json.tool
```

### 3. Configure the agent's own env

```sh
cp .env.example .env
# Optional: set TAVILY_API_KEY for the better search backend.
# LLM_GATEWAY_V3_URL defaults to http://localhost:8101 — leave as-is unless you changed the gateway port.
```

### 4. (When `agent6.py` lands) Run a query

```sh
# Coming after the cognitive layer code is written:
uv run python agent6.py "what's the time in Tokyo?"
```

`agent6.py` will spawn `mcp_server.py` as a stdio subprocess on every invocation; you don't need to start the MCP server manually.

---

## Cleaning state between attempts

`state/` and `sandbox/` are gitignored. To reset durable memory + the file-tool sandbox between runs:

```sh
rm -rf state/ sandbox/ usage.json
```

This is the assignment-mandated cleanability story.

---

## Open work — what blocks the next step

See PLAN.md Section 9 — the only remaining blocker is **the 4 target queries** (text, expected answers, expected iteration counts). Once those arrive, `schemas.py` lands first (direct transcription of your provided contracts), then the four layers, then prompt-tuning.

The mechanical scaffolding above is already in place; the cognitive layers come next.
