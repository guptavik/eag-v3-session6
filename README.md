# EAG v3 — Session 6: Four-Layer Cognitive Agent

A tool-using agent built as four cooperating cognitive layers — **perception → memory → decision → action** — that:

- Uses the [LLM Gateway V3](mcp-server/llm_gatewayV3/) as its sole LLM substrate (no direct provider SDKs).
- Talks to a stdio MCP server with 9 general-purpose tools.
- Persists durable memory + content-addressable artifacts in `state/`, cleanable with `rm -rf state/`.
- Validates every layer boundary with Pydantic v2 contracts (`schemas.py`); no regex on LLM output.

All four target queries pass within the iteration cap. See [Results](#results) below.

---

## Layout

```
.
├── agent6.py                main loop: memory.remember → for iter → memory.read
│                            → perception.observe → force_attach → attach
│                            → decision.next → action.execute → memory.add
├── perception.py            initial decomposer + per-iter refresher + force-attach
├── memory.py                AgentMemory (durable list) + ArtifactStore (sha256-CAS)
├── decision.py              one Gemini call per iter → DecisionOutput XOR(answer, tool_call)
├── action.py                stdio MCP client + artifact-handle offload (>4KB)
├── schemas.py               Pydantic v2 contracts for every layer boundary
├── _gateway_path.py         sys.path shim so `from llm_gatewayV3.client import LLM` works
│
├── mcp_server.py            9-tool MCP server (stdio transport)
├── PLAN.md                  implementation plan (kept for reference)
├── pyproject.toml           uv-managed deps
├── logs/                    per-query traces (query-a.log, query-b.log, ...)
├── state/                   durable memory + artifacts; gitignored; wipeable
│
├── mcp-server/
│   └── llm_gatewayV3/       FastAPI service on :8101 — every LLM call goes here
│
├── mcp-server-meeting-intel/   PARKED prior Session-5/6 meeting-intel server
└── extension-meeting-intel/    PARKED prior Chrome extension UI
```

The `*-meeting-intel/` folders preserve earlier work and aren't part of this assignment.

---

## The 9 tools (in `mcp_server.py`)

| Tool | What it does |
|---|---|
| `web_search(query, max_results=5)` | Tavily primary, DuckDuckGo fallback. Hard-capped at 5 results. Usage logged to `./usage.json`, soft-capped at 950/1000 on Tavily. |
| `fetch_url(url, timeout=60)` | Headless-Chromium fetch via crawl4ai → clean markdown. 60s hard cap. |
| `get_time(timezone="UTC")` | Current time in a named IANA timezone (requires `tzdata` on Windows). |
| `currency_convert(amount, from, to)` | ISO-3 conversion via frankfurter.dev. |
| `read_file(path)` | UTF-8 read from `./sandbox/`. |
| `list_dir(path=".")` | Directory listing under `./sandbox/`. |
| `create_file(path, content)` | Create new file in sandbox (errors if exists). |
| `update_file(path, content)` | Overwrite existing sandbox file. |
| `edit_file(path, find, replace, replace_all=False)` | Find-and-replace inside a sandbox file. |

All file ops are sandboxed under `./sandbox/`; path traversal raises `ValueError`.

---

## Architecture

```
User query
    │
    ▼  memory.remember()              ← one-shot LLM call; persists facts/prefs
    │
    ▼  for each iter (cap 16):
    │
    ▼  memory.read(observation)       → MemoryItem[] (durable in state/memory.json)
    │
    ▼  perception.observe()           → Observation(goals=[Goal, ...])
    │                                   + force_attach() safety net for synthesis goals
    │
    ▼  decision.next()                → DecisionOutput(answer XOR tool_call)
    │
    ▼  action.execute()               → ActionResult  (stdio MCP call)
    │                                   payloads >4KB → ArtifactStore (sha256-CAS)
    │
    └── memory.add(tool_outcome) → next iter, until DecisionOutput.answer for last goal
```

Pydantic contracts (`MemoryItem`, `Artifact`, `Goal`, `Observation`, `ToolCall`, `DecisionOutput`) are typed end-to-end. `DecisionOutput`'s `model_validator(mode="after")` enforces the XOR contract — exactly one of `answer | tool_call` is non-null per iteration, or the loop refuses to dispatch.

The Decision LLM call uses `auto_route="decision"` so the gateway selects the appropriate tier based on prompt size and complexity.

---

## Setup

### 1. Install Python deps

```sh
# Once, if you don't have uv:
#   curl -LsSf https://astral.sh/uv/install.sh | sh                                   # macOS / Linux
#   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows

uv sync
uv run python -m playwright install chromium       # required by fetch_url
```

### 2. Start the LLM Gateway

```sh
cd mcp-server/llm_gatewayV3
./run.sh                  # listens on http://localhost:8101
```

Provider keys live in `mcp-server/.env` (gemini, groq, cerebras, openrouter, nvidia, github). At minimum one must be set; the gateway auto-routes between available tiers.

Health check:
```sh
curl -s http://localhost:8101/v1/providers | python -m json.tool
```

### 3. Configure the agent's env

```sh
cp .env.example .env
# Optional: set TAVILY_API_KEY for higher-quality web_search results.
```

### 4. Run a query

```sh
uv run python agent6.py "What's the time in Tokyo?"
```

`agent6.py` spawns `mcp_server.py` as a stdio subprocess on every invocation; the MCP server doesn't need to be started manually.

---

## Cleaning state between attempts

`state/` and `sandbox/` are gitignored. To reset durable memory + the file-tool sandbox:

```sh
rm -rf state/ sandbox/ usage.json
```

---

## Results

All four target queries pass within the iteration cap of 16. Full traces are in `logs/`.

### Query A — Wikipedia lookup → 5 iterations

```sh
uv run python agent6.py "Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory."
```

Trace shape (from [logs/query-a.log](logs/query-a.log)):

```
─── iter 1 ───   web_search("Claude Shannon Wikipedia") → snippet
─── iter 2 ───   decision XOR-error → recovery via scratchpad memory item
─── iter 3 ───   fetch_url(...) → artifact art:357535cbe22f359f (263 KB)
─── iter 4 ───   force-attach art:357535cbe22f359f → ANSWER from artifact
─── iter 5 ───   all 2 goals satisfied → FINAL
```

**FINAL:** Birth Apr 30 1916, death Feb 24 2001, three contributions named (formalization of information theory, entropy, data compression).

### Query B — Multi-tool synthesis → 5 iterations

```sh
uv run python agent6.py "I am thinking of going to Tokyo. What activities and events are happening? Also tell me what time and weather is there now."
```

```
memory.remember     classified "The user is considering a trip to Tokyo." as fact
─── iter 1 ───      get_time(Asia/Tokyo) → 2026-05-17 05:49 JST
─── iter 2 ───      web_search("current weather in Tokyo")
─── iter 3 ───      web_search("major events and festivals in Tokyo May 2026")
─── iter 4 ───      ANSWER (synthesised from inline snippets)
─── iter 5 ───      all 3 goals satisfied → FINAL
```

**FINAL:** Tokyo time (Sunday 17 May 2026 05:49 JST), weather summary, Kanda Matsuri + flower-viewing events with a citation link.

### Query C — Durable memory across runs → 2 + 2 iterations

```sh
# Run 1 — durable write
uv run python agent6.py "Hi please remember my mom's birthday is on 23rd September."
```

```
memory.remember     classified "The user's mother has a birthday on September 23." as fact
─── iter 1 ───      ANSWER: Yes, confirmed.
─── iter 2 ───      all 1 goals satisfied → FINAL
```

`state/memory.json` after run 1:
```json
{
  "id": "mem:00c00fdbacff",
  "kind": "fact",
  "keywords": ["mom", "birthday", "september", "23rd"],
  "value": {"person": "mother", "day": 23, "month": "September"},
  "source": "user_statement"
}
```

```sh
# Run 2 — durable read (NO state wipe between runs)
uv run python agent6.py "When is my mom's birthday?"
```

```
─── iter 1 ───      memory.read → 1 hit from state/memory.json
                    ANSWER: The user's mother's birthday is September 23.
─── iter 2 ───      all 1 goals satisfied → FINAL
```

No tool calls in run 2 — the answer comes entirely from durable memory written by run 1.

### Query D — Multi-source research synthesis → 10 iterations

```sh
uv run python agent6.py "I want to write a tutorial about Python's asyncio. Find me three credible references about asyncio (any of: the official docs, a high-quality blog or article, a video transcript or talk summary), then summarize the most important points from each in 2-3 bullets and tell me which to cite first."
```

```
─── iter 1 ───      web_search(bundled) → RealPython hit
─── iter 2 ───      web_search(site:docs.python.org) → official docs URL
─── iter 3 ───      web_search(youtube transcript) → YouTube tutorial URL
─── iter 4 ───      fetch_url(docs.python.org) → art:65c89f25eaca81b7 (10 KB)
─── iter 5 ───      ANSWER (three references listed)
─── iter 6 ───      force-attach docs → fetch_url(realpython) → art:c89a00e57dad6da7 (79 KB)
─── iter 7 ───      ANSWER (per-source summaries)
─── iter 8 ───      ANSWER (citation order)
─── iter 9 ───      ANSWER (consolidated)
─── iter 10 ───     all 4 goals satisfied → FINAL
```

**FINAL:** Three references summarised with 2-3 bullets each. Citation order: official docs → RealPython walkthrough → YouTube tutorial, with rationale.

---

## Iteration counts vs. expected

| Query | Expected | Actual | Cap |
|-------|----------|--------|-----|
| A — Shannon Wikipedia | 3 | 5 | 16 |
| B — Tokyo trip | 6 | 5 | 16 |
| C run 1 — write birthday | 2 | 2 | 16 |
| C run 2 — recall birthday | 2 | 2 | 16 |
| D — asyncio research | 5–7 | 10 | 16 |

Query A burned 2 extra iters (web_search before fetch_url + one Decision XOR-validation error). Query D burned a few extra iters re-emitting answers as Perception kept the bundled retrieve-and-summarise goal open. Both are within the 16 cap.

---

## Implementation notes — non-obvious bits

- **Decision XOR contract.** `DecisionOutput` rejects any output where both `answer` and `tool_call` are set or both are null. The loop catches the `ValueError`, writes a `scratchpad` memory item describing the failure, and continues — burning at most 1 iter on a bad LLM response. After 3 consecutive Decision failures the run bails out.
- **Force-attach safety net.** Perception scans each open goal text for synthesis keywords (`extract`, `summarise`, `compare`, `list`, `decide`, ...). When matched, the loop picks the most recent unassigned artifact and sets `goal.attach_artifact_id` — the agent6 loop then loads the bytes (truncated to 24 KB) and prepends them to the Decision prompt, so Decision can answer from the artifact without re-calling `fetch_url`.
- **No `response_format`.** The gateway validates with strict JSON Schema (rejects OpenAPI `nullable: true`), while the Gemini worker requires OpenAPI-style schemas (rejects union types like `{"type":["string","null"]}`). Going through `response_format` produces a 5xx either way. Instead Perception, Memory.remember, and Decision pull plain text and parse manually with `json.loads` + `model_validate` — Pydantic still enforces every contract; no regex.
- **Gemini-3 at temperature 0.0 loops on schema-constrained calls.** All three LLM-calling layers use `temperature=1.0`.
- **Windows + crawl4ai.** crawl4ai's Rich logger writes box-drawing chars; the child must use UTF-8 or it silently hangs mid-fetch. `mcp_server.py` reconfigures `sys.stdout/sys.stderr` to UTF-8 at startup, and `action.py` sets `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` in the spawned MCP server's env. `crawl4ai` is imported at module top so any first-import cost is paid before the FastMCP loop starts servicing requests.
- **Artifact threshold = 4 KB.** Tool payloads above this size get offloaded to `state/artifacts/<sha256>.{bin,json}` and the inline `ActionResult.result` becomes a short preview + `artifact_id`. Keeps the Decision prompt small even when fetching 260 KB Wikipedia pages.

---

## Cleaning state between attempts

```sh
rm -rf state/ sandbox/ usage.json
```

`state/memory.json` accumulates across runs by design. To rerun Query C as a true durable test, wipe state before run 1 and do **not** wipe between run 1 and run 2.
