# EAG v3 — Session 6: Four-Layer Cognitive Agent

A tool-using agent built as four cooperating cognitive layers — **perception → memory → decision → action** — that:

- Uses the [LLM Gateway V3](api/mcp-server/llm_gatewayV3/) as its sole LLM substrate (no direct provider SDKs).
- Talks to a stdio MCP server with 9 general-purpose tools.
- Persists durable memory + content-addressable artifacts in `api/state/`, cleanable with `rm -rf api/state/`.
- Validates every layer boundary with Pydantic v2 contracts (`api/schemas.py`); no regex on LLM output.

All four target queries pass within the assignment's 2× iteration cap. See [Results](#results).

---

## Layout

```
.
├── api/                                 all Python lives here
│   ├── agent6.py                        main loop: memory.remember → for iter
│   │                                    → memory.read → perception.observe
│   │                                    → force_attach → attach → decision.next
│   │                                    → action.execute → memory.add
│   ├── perception.py                    initial decomposer + per-iter refresher + force-attach
│   ├── memory.py                        AgentMemory (durable list) + ArtifactStore (sha256-CAS)
│   ├── decision.py                      one LLM call per iter → DecisionOutput XOR(answer, tool_call)
│   ├── action.py                        stdio MCP client + artifact-handle offload (>4KB)
│   ├── schemas.py                       Pydantic v2 contracts for every layer boundary
│   ├── _gateway_path.py                 sys.path shim so `from llm_gatewayV3.client import LLM` works
│   ├── mcp_server.py                    9-tool MCP server (stdio transport)
│   ├── .env.example                     agent env template (TAVILY_API_KEY, gateway URL)
│   ├── state/                           durable memory + artifacts; gitignored; wipeable
│   ├── sandbox/                         mcp_server's file-tool sandbox; gitignored
│   └── mcp-server/
│       └── llm_gatewayV3/               FastAPI service on :8101 — every LLM call goes here
│
├── PLAN.md                              implementation plan (kept for reference)
├── pyproject.toml                       uv-managed deps; lives at repo root
├── logs/                                per-query traces (query-a.log, query-b.log, ...)
└── README.md                            this file
```

---

## The 9 tools (in `api/mcp_server.py`)

| Tool | What it does |
|---|---|
| `web_search(query, max_results=5)` | Tavily primary, DuckDuckGo fallback. Hard-capped at 5 results. Usage logged to `api/usage.json`, soft-capped at 950/1000 on Tavily. |
| `fetch_url(url, timeout=60)` | Headless-Chromium fetch via crawl4ai → clean markdown. 60s hard cap. |
| `get_time(timezone="UTC")` | Current time in a named IANA timezone (requires `tzdata` on Windows). |
| `currency_convert(amount, from, to)` | ISO-3 conversion via frankfurter.dev. |
| `read_file(path)` | UTF-8 read from `api/sandbox/`. |
| `list_dir(path=".")` | Directory listing under `api/sandbox/`. |
| `create_file(path, content)` | Create new file in sandbox; auto-mkdir intermediate dirs; errors if file exists. |
| `update_file(path, content)` | Overwrite existing sandbox file. |
| `edit_file(path, find, replace, replace_all=False)` | Find-and-replace inside a sandbox file. |

All file ops are sandboxed under `api/sandbox/`; path traversal raises `ValueError`.

---

## Architecture

```
User query
    │
    ▼  memory.remember()              ← one-shot LLM call; persists facts/prefs
    │
    ▼  for each iter (cap 16):
    │
    ▼  memory.read(observation)       → MemoryItem[] (durable in api/state/memory.json)
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
cd api/mcp-server/llm_gatewayV3
./run.sh                  # listens on http://localhost:8101
```

Provider keys live in `api/mcp-server/.env` (gemini, groq, cerebras, openrouter, nvidia, github). At minimum one must be set; the gateway auto-routes between available tiers.

Health check:
```sh
curl -s http://localhost:8101/v1/providers | python -m json.tool
```

### 3. Configure the agent's env

```sh
cp api/.env.example api/.env
# Optional: set TAVILY_API_KEY for higher-quality web_search results.
```

### 4. Run a query

```sh
uv run python api/agent6.py "What's the time in Tokyo?"
```

`api/agent6.py` spawns `api/mcp_server.py` as a stdio subprocess on every invocation; the MCP server doesn't need to be started manually.

---

## Cleaning state between attempts

```sh
rm -rf api/state/ api/sandbox/ api/usage.json
```

`api/state/memory.json` accumulates across runs by design. To rerun Query C as a true durable test, wipe state before run 1 and do **not** wipe between run 1 and run 2.

---

## Perception, Decision, and Memory Prompts (PoP)

The system prompts below are class constants on the respective layer modules; the README quotes them verbatim so reviewers don't have to chase them through the code. Source: `Perception.INITIAL_SYSTEM_PROMPT` / `Perception.REFRESH_SYSTEM_PROMPT` in [api/perception.py](api/perception.py), `Decision.SYSTEM_PROMPT` in [api/decision.py](api/decision.py), and `AgentMemory.REMEMBER_SYSTEM_PROMPT` in [api/memory.py](api/memory.py).

### Perception — initial decomposition (iter 1)

```
You are the perception layer of a tool-using agent. Given the user's query
and any prior memory the agent has, decompose the request into an ordered
list of GOALS. Each goal must be one concrete unit of work that can be
solved by either calling a tool or by composing prior results.

Guidelines:
1. Keep goal text short and imperative ("Fetch the Wikipedia page for X",
   "Extract X from the fetched page", "Choose the best option given X").
2. The LAST goal must be the one whose answer is shown to the user.
3. If a fact in memory already answers the query, emit a single synthesis
   goal — do not request a tool call you don't need.
4. Do NOT decide which tool to use. Just describe what each goal needs.
5. Number of goals: 1 for simple queries, 2-4 for multi-step.
6. Bundling rule: when the user asks for several related pieces of
   information in a single conjunctive sentence ("X, Y, and Z"), emit ONE
   extraction goal that names all of them — not separate goals per item.
   Example: "Extract X, Y, and Z from the fetched page" is one goal, not three.
7. Persist-data rule: when the user asks to remember, save, record, or
   'give me a reminder' for a value, emit (a) one or more goals that each
   start with "Create a file at <path>" so the Decision layer dispatches
   create_file with concrete dates, AND (b) a final goal like "Confirm
   reminders have been saved and summarise what was stored" — because rule 2
   still applies: the LAST goal must produce the textual answer shown to
   the user, and a tool_call alone is not an answer.
8. Date math: if the user gives a relative date phrase ("two weeks before",
   "the day after", "next Friday"), resolve it to an absolute YYYY-MM-DD
   in the goal text so downstream layers don't have to reason about it again.

Return JSON: {"goals": [{"text": "..."}]}. Nothing else, no markdown,
no commentary.
```

### Perception — refresh (iter ≥ 2)

```
You are the perception layer reviewing progress on an in-flight agent run.
Given the prior goals and the most recent tool outcomes, decide for each
goal whether it is now DONE. Be conservative: only mark a goal done if the
evidence in the recent action results actually fulfils it.

Rules:
1. Goals already marked done STAY done (sticky-done invariant).
2. A goal whose text says "fetch X" becomes done once a successful
   fetch_url result for X appears in the recent actions.
3. A goal whose text says "search for X" becomes done once a successful
   web_search result for X appears.
4. A goal whose text says "answer", "choose", "extract", "list", "compare",
   "summarise" etc. is NEVER done by a tool call alone — it is only marked
   done when the synthesis happens (which is the Decision layer's job,
   not yours).

Return JSON: {"goals": [{"id": "goal:xxx", "text": "...", "done": true|false}]}.
Preserve goal `id` and `text` exactly as given. Do not add or remove goals.
```

### Decision

```
You are the decision layer of a tool-using agent. You see exactly one
open GOAL at a time and must decide what to do next.

Available actions on each turn:
 (a) emit a `tool_call` — when you need more information to make progress
     on the goal. Pick exactly one tool from the catalogue and provide JSON
     arguments matching its schema.
 (b) emit an `answer` — when the data you already have is enough to satisfy
     the current goal. The answer must be the human-readable text for this
     goal (this goal only — later goals get their own turn).

Hard rules:
1. Output MUST be exactly one JSON object with two keys: `answer` and
   `tool_call`. EXACTLY ONE of them is non-null; the other is null.
   Emit nothing else — no prose, no markdown fences, no commentary.
2. Do not invent tool names. Use only the names listed in the Tool
   catalogue section of the user message.
3. For goals shaped like "fetch X" or "search for X", emit a tool_call.
   For goals shaped like "extract / list / choose / compare / answer",
   emit an answer once the needed data is present (either in memory hits
   or in the ATTACHED ARTIFACT).
4. If an artifact is ATTACHED below the prompt, read it and use its
   content — DO NOT call fetch_url for the same URL again.
5. Never fabricate facts. If the data is genuinely missing, emit a
   tool_call to get it.
6. When the current goal text contains a fully-qualified URL (https://…
   or http://…), prefer `fetch_url` over `web_search` — the page is
   already named, there is nothing to search for.
7. When the goal text starts with "Create a file", "Save", "Record", or
   otherwise asks to persist data, emit a `create_file` tool_call with a
   sensible sandbox path (reminders/<slug>.txt, notes/<slug>.md, etc.)
   and a body that captures the fact. Use `update_file` only if the path
   already exists in memory hits or recent action results.
```

### Memory.remember — durable-fact classifier

```
You are a memory-extraction classifier for an agent. Given the user's
message, decide whether it contains any DURABLE fact or preference the
agent should remember across runs. A durable fact is a date, a name, a
relationship, a stated preference, or any other piece of information the
user explicitly tells the agent to remember.

Return a JSON object matching this schema:
{ "items": [ { "kind": "fact"|"preference",
               "keywords": [<lowercase strings>],
               "descriptor": "<one human-readable sentence>",
               "value": <JSON object capturing the structured payload> } ] }

If the user is only asking a question or issuing a non-declarative command,
return {"items": []}. Do not invent facts.
```

---

## Validation JSON (Pydantic contracts)

Every layer boundary is typed by a Pydantic v2 model. Below is the JSON Schema for each (generated by `Model.model_json_schema()`); these are what the Pydantic validators check on every cross-layer call. Source: [api/schemas.py](api/schemas.py).

### Goal

```json
{
  "type": "object",
  "title": "Goal",
  "additionalProperties": false,
  "properties": {
    "id": {"type": "string"},
    "text": {"type": "string"},
    "done": {"type": "boolean", "default": false},
    "attach_artifact_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null}
  },
  "required": ["id", "text"]
}
```

### Observation (Perception output)

```json
{
  "type": "object",
  "title": "Observation",
  "additionalProperties": false,
  "properties": {
    "goals": {"type": "array", "items": {"$ref": "#/$defs/Goal"}}
  },
  "required": ["goals"]
}
```

### ToolCall

```json
{
  "type": "object",
  "title": "ToolCall",
  "additionalProperties": false,
  "properties": {
    "name": {"type": "string"},
    "arguments": {"type": "object", "additionalProperties": true}
  },
  "required": ["name"]
}
```

### DecisionOutput (XOR — enforced by `model_validator`)

```json
{
  "type": "object",
  "title": "DecisionOutput",
  "additionalProperties": false,
  "properties": {
    "answer":    {"anyOf": [{"type": "string"},                  {"type": "null"}], "default": null},
    "tool_call": {"anyOf": [{"$ref": "#/$defs/ToolCall"},        {"type": "null"}], "default": null}
  }
}
```

Validation rule (`schemas.py:DecisionOutput.either_answer_or_tool`):
```python
@model_validator(mode="after")
def either_answer_or_tool(self) -> DecisionOutput:
    a = self.answer is not None
    t = self.tool_call is not None
    if a == t:
        raise ValueError("DecisionOutput requires exactly one of {answer, tool_call}")
    return self
```

### MemoryItem (durable memory unit)

```json
{
  "type": "object",
  "title": "MemoryItem",
  "additionalProperties": false,
  "properties": {
    "id":          {"type": "string"},
    "kind":        {"type": "string", "enum": ["fact", "preference", "tool_outcome", "scratchpad"]},
    "keywords":    {"type": "array", "items": {"type": "string"}},
    "descriptor":  {"type": "string"},
    "value":       {"type": "object", "additionalProperties": true},
    "artifact_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "source":      {"type": "string"},
    "run_id":      {"type": "string"},
    "goal_id":     {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
    "confidence":  {"type": "number", "minimum": 0.0, "maximum": 1.0},
    "created_at":  {"type": "string", "format": "date-time"}
  },
  "required": ["id", "kind", "keywords", "descriptor", "value", "source", "run_id", "confidence", "created_at"]
}
```

### Artifact (content-addressed blob metadata)

```json
{
  "type": "object",
  "title": "Artifact",
  "additionalProperties": false,
  "properties": {
    "id":           {"type": "string"},
    "content_type": {"type": "string"},
    "size_bytes":   {"type": "integer"},
    "source":       {"type": "string"},
    "descriptor":   {"type": "string"}
  },
  "required": ["id", "content_type", "size_bytes", "source", "descriptor"]
}
```

---

## Results

All four target queries were re-run from clean state on the local machine; the captured terminal output is reproduced below verbatim and lives in [logs/](logs/) for re-inspection. Iteration counts are summarized in the table after the per-query sections.

### Query A — Wikipedia lookup → 3 iterations

```sh
rm -rf api/state api/sandbox
uv run python api/agent6.py "Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his birth date, death date, and three key contributions to information theory."
```

Full terminal output ([logs/query-a.log](logs/query-a.log)):

```
[05/17/26 14:09:56] INFO     Processing request of type           server.py:727
                             ListToolsRequest

─── iter 1 ───
[memory.read]   0 hits
[perception]    [open] Fetch the Wikipedia page for Claude Shannon
                [open] Extract his birth date, death date, and three key contributions to information theory from the fetched page
[decision]      TOOL_CALL: fetch_url({"url": "https://en.wikipedia.org/wiki/Claude_Shannon"})
[05/17/26 14:10:36] INFO     Processing request of type           server.py:727
                             CallToolRequest
[INIT].... → Crawl4AI 0.8.6
[FETCH]... ↓ https://en.wikipedia.org/wiki/Claude_Shannon
| ✓ | ⏱: 2.50s
[SCRAPE].. ◆ https://en.wikipedia.org/wiki/Claude_Shannon
| ✓ | ⏱: 0.91s
[COMPLETE] ● https://en.wikipedia.org/wiki/Claude_Shannon
| ✓ | ⏱: 3.46s
[action]        → [artifact art:9ffe12b52cac72dc, see memory] preview: {
  "status": 200,
  "content_type": "text/markdown",
  "length_bytes": 255130,
…

─── iter 2 ───
[memory.read]   1 hits
                tool_outcome: fetch_url → ok
[perception]    [done] Fetch the Wikipedia page for Claude Shannon
                [open] Extract his birth date, death date, and three key contributions to information theory from the fetched page
                  attach=art:9ffe12b52cac72dc
[attach]        art:9ffe12b52cac72dc (260981 bytes)
[decision]      ANSWER: Claude Shannon was born on April 30, 1916, and died on February 24, 2001. Three of his key contributions to information theory include: 1. The establishment of information theory as a formal field of study. 2. The development of the concept of entropy in infor…

─── iter 3 ───
[memory.read]   1 hits
                tool_outcome: fetch_url → ok
[perception]    [done] Fetch the Wikipedia page for Claude Shannon
                [done] Extract his birth date, death date, and three key contributions to information theory from the fetched page

[done] all 2 goals satisfied

FINAL: Claude Shannon was born on April 30, 1916, and died on February 24, 2001. Three of his key contributions to information theory include:
1. The establishment of information theory as a formal field of study.
2. The development of the concept of entropy in information theory.
3. The application of Boolean algebra to the design of digital logic circuits (switching circuit theory).
```

Iter 1 calls `fetch_url` directly (Decision rule 6 — URL already named). Iter 2 attaches the 261 KB artifact to the bundled extraction goal (Perception bundling rule), Decision answers from the artifact bytes. Iter 3 confirms all goals done.

### Query B — Multi-tool synthesis → 5 iterations

```sh
uv run python api/agent6.py "Find 3 family-friendly things to do in Tokyo this weekend. Check Saturday's weather forecast there and tell me which one is most appropriate."
```

Full terminal output ([logs/query-b.log](logs/query-b.log)):

```
[05/17/26 14:11:44] INFO     Processing request of type           server.py:727
                             ListToolsRequest

─── iter 1 ───
[memory.read]   0 hits
[perception]    [open] Fetch a list of family-friendly things to do in Tokyo this weekend
                [open] Fetch Saturday's weather forecast for Tokyo
                [open] Select the most appropriate activity from the list based on the weather forecast
[decision]      TOOL_CALL: web_search({"query": "family-friendly things to do in Tokyo this weekend"})
[action]        → {"title": "Tokyo With Kids – 53 Things To Do Guide + Photos - Plan My Japan", "url": "https://www.planmyjapan.com/tokyo-with-kids-things-to-do/", "snippet"…

─── iter 2 ───
[memory.read]   2 hits
                tool_outcome: web_search → ok
[perception]    [done] Fetch a list of family-friendly things to do in Tokyo this weekend
                [open] Fetch Saturday's weather forecast for Tokyo
                [open] Select the most appropriate activity from the list based on the weather forecast
[decision]      TOOL_CALL: web_search({"query": "weather forecast Tokyo this Saturday"})
[action]        → {"title": "Tokyo, Tokyo, Japan Weather Forecast | AccuWeather", "url": "https://www.accuweather.com/en/jp/tokyo/226396/weather-forecast/226396", "snippet": "Tok…

─── iter 3 ───
[memory.read]   3 hits
                tool_outcome: web_search → ok
                tool_outcome: web_search → ok
[perception]    [done] Fetch a list of family-friendly things to do in Tokyo this weekend
                [done] Fetch Saturday's weather forecast for Tokyo
                [open] Select the most appropriate activity from the list based on the weather forecast
[decision]      TOOL_CALL: fetch_url({"url": "https://www.planmyjapan.com/tokyo-with-kids-things-to-do/"})
[INIT].... → Crawl4AI 0.8.6
[FETCH]... ↓ https://www.planmyjapan.com/tokyo-with-kids-things-to-do/
| ✓ | ⏱: 7.03s
[SCRAPE].. ◆ https://www.planmyjapan.com/tokyo-with-kids-things-to-do/
| ✓ | ⏱: 0.51s
[COMPLETE] ● https://www.planmyjapan.com/tokyo-with-kids-things-to-do/
| ✓ | ⏱: 7.61s
[action]        → [artifact art:155565d03667f9fa, see memory] preview: {
  "status": 200,
  "content_type": "text/markdown",
  "length_bytes": 99150,
 …

─── iter 4 ───
[memory.read]   3 hits
                tool_outcome: web_search → ok
                tool_outcome: web_search → ok
                tool_outcome: fetch_url → ok
[perception]    [done] Fetch a list of family-friendly things to do in Tokyo this weekend
                [done] Fetch Saturday's weather forecast for Tokyo
                [open] Select the most appropriate activity from the list based on the weather forecast
[decision]      ANSWER: Given that Saturday's weather in Tokyo calls for light rain and overcast conditions, an indoor family-friendly activity is the best choice. Based on the guide, visiting the Sunshine City complex in Ikebukuro is the most appropriate option. It features the Suns…

─── iter 5 ───
[memory.read]   3 hits
                tool_outcome: web_search → ok
                tool_outcome: web_search → ok
                tool_outcome: fetch_url → ok
[perception]    [done] Fetch a list of family-friendly things to do in Tokyo this weekend
                [done] Fetch Saturday's weather forecast for Tokyo
                [done] Select the most appropriate activity from the list based on the weather forecast

[done] all 3 goals satisfied

FINAL: Given that Saturday's weather in Tokyo calls for light rain and overcast conditions, an indoor family-friendly activity is the best choice. Based on the guide, visiting the Sunshine City complex in Ikebukuro is the most appropriate option. It features the Sunshine Aquarium and the Namjatown indoor theme park, both of which are excellent for families and provide a complete indoor experience to avoid the wet weather.
```

3 goals → 5 iters (1 search per source + 1 fetch for activity detail + 1 synthesis ANSWER + 1 confirmation). Force-attach did not fire — there were no stale artifacts whose keywords matched "Tokyo" / "activity" / "weather".

### Query C run 1 — durable write + sandbox file → 3 iterations

```sh
rm -rf api/state api/sandbox
uv run python api/agent6.py "My mom's birthday is 15 May 2026. Remember that and give me a calendar reminder for two weeks before and on the day."
```

Full terminal output ([logs/query-c1.log](logs/query-c1.log)):

```
[memory.remember]  classified "The user's mother's birthday is on 15 May 2026." as fact
                   keywords: [mom, birthday, date]
[05/17/26 14:24:19] INFO     Processing request of type           server.py:727
                             ListToolsRequest

─── iter 1 ───
[memory.read]   1 hits
                fact: The user's mother's birthday is on 15 May 2026.
2026-05-17 14:24:33,429 WARNING perception: perception: malformed initial JSON, falling back to single goal
[perception]    [open] My mom's birthday is 15 May 2026. Remember that and give me a calendar reminder for two weeks before and on the day.
[decision]      TOOL_CALL: create_file({"path": "reminders/moms_birthday.txt", "content": "Mom's Birthday: 15 May 2026\nReminder 1 (Two weeks before): 1 May 2026\nReminder 2 (Birthday): 15 May 2026"})
[action]        → {"ok": true, "path": "reminders/moms_birthday.txt", "size_bytes": 106}

─── iter 2 ───
[memory.read]   2 hits
                tool_outcome: create_file → ok
                fact: The user's mother's birthday is on 15 May 2026.
[perception]    [open] My mom's birthday is 15 May 2026. Remember that and give me a calendar reminder for two weeks before and on the day.
[decision]      ANSWER: I have successfully saved the reminders for your mother's birthday. You have a reminder set for 1 May 2026 (two weeks before) and 15 May 2026 (the day of).

─── iter 3 ───
[memory.read]   2 hits
                tool_outcome: create_file → ok
                fact: The user's mother's birthday is on 15 May 2026.
[perception]    [done] My mom's birthday is 15 May 2026. Remember that and give me a calendar reminder for two weeks before and on the day.

[done] all 1 goals satisfied

FINAL: I have successfully saved the reminders for your mother's birthday. You have a reminder set for 1 May 2026 (two weeks before) and 15 May 2026 (the day of).
```

`api/state/memory.json` after run 1:
```json
{
  "id": "mem:340b563599f3",
  "kind": "fact",
  "keywords": ["mom", "birthday", "date"],
  "descriptor": "The user's mother's birthday is on 15 May 2026.",
  "value": {"relation": "mother", "date": "2026-05-15", "event": "birthday"},
  "source": "user_statement",
  "confidence": 0.95
}
```

`api/sandbox/reminders/moms_birthday.txt` after run 1:
```
Mom's Birthday: 15 May 2026
Reminder 1 (Two weeks before): 1 May 2026
Reminder 2 (Birthday): 15 May 2026
```

The Perception JSON came back malformed on iter 1 and the loop fell back to single-goal mode (raw user query as the goal text). Decision still recognized the persist verbs and called `create_file` with both dates resolved; the file was written under the nested `reminders/` path (auto-mkdir).

### Query C run 2 — durable read (NO state wipe) → 2 iterations

```sh
uv run python api/agent6.py "When is mom's birthday?"
```

Full terminal output ([logs/query-c2.log](logs/query-c2.log)):

```
[05/17/26 14:26:11] INFO     Processing request of type           server.py:727
                             ListToolsRequest

─── iter 1 ───
[memory.read]   2 hits
                tool_outcome: create_file → ok
                fact: The user's mother's birthday is on 15 May 2026.
[perception]    [open] Answer: Mom's birthday is on 15 May 2026.
[decision]      ANSWER: Mom's birthday is on 15 May 2026.

─── iter 2 ───
[memory.read]   2 hits
                tool_outcome: create_file → ok
                fact: The user's mother's birthday is on 15 May 2026.
[perception]    [done] Answer: Mom's birthday is on 15 May 2026.

[done] all 1 goals satisfied

FINAL: Mom's birthday is on 15 May 2026.
```

Zero tool calls — the answer comes entirely from the durable fact persisted by run 1.

### Query D — Multi-source research synthesis → 7 iterations

```sh
rm -rf api/state api/sandbox
uv run python api/agent6.py "Search for 'Python asyncio best practices', read the top 3 results, and give me a short numbered list of the advice they agree on."
```

Full terminal output ([logs/query-d.log](logs/query-d.log)):

```
[05/17/26 14:27:25] INFO     Processing request of type           server.py:727
                             ListToolsRequest

─── iter 1 ───
[memory.read]   0 hits
[perception]    [open] Search for 'Python asyncio best practices'
                [open] Fetch the content of the top 3 search results
                [open] Identify common advice across the top 3 results and compile them into a numbered list
[decision]      TOOL_CALL: web_search({"query": "Python asyncio best practices", "max_results": 3})
[action]        → {"title": "Master asyncio in Python: A Comprehensive Step-by-Step... | Medium", "url": "https://medium.com/pythoniq/master-asyncio-in-python-a-comprehensive-ste…

─── iter 2 ───
[memory.read]   1 hits
                tool_outcome: web_search → ok
[perception]    [done] Search for 'Python asyncio best practices'
                [open] Fetch the content of the top 3 search results
                [open] Identify common advice across the top 3 results and compile them into a numbered list
[decision]      TOOL_CALL: fetch_url({"url": "https://medium.com/pythoniq/master-asyncio-in-python-a-comprehensive-step-by-step-guide-4fc2cfa49925"})
[INIT].... → Crawl4AI 0.8.6
[FETCH]... ↓ https://medium.com/pythoniq/master-asyncio-in-python-a-comprehensive-step-by-step-guide-4fc2cfa49925 | ✓ | ⏱: 4.02s
[SCRAPE].. ◆ https://medium.com/pythoniq/master-asyncio-in-python-a-comprehensive-step-by-step-guide-4fc2cfa49925 | ✓ | ⏱: 0.02s
[COMPLETE] ● https://medium.com/pythoniq/master-asyncio-in-python-a-comprehensive-step-by-step-guide-4fc2cfa49925 | ✓ | ⏱: 4.07s
[action]        → {"status": 200, "content_type": "text/markdown", "length_bytes": 1297, "text": "[Sitemap](https://medium.com/sitemap/sitemap.xml)\n[Open in app](https://play.go…

─── iter 3 ───
[memory.read]   2 hits
                tool_outcome: web_search → ok
                tool_outcome: fetch_url → ok
[perception]    [done] Search for 'Python asyncio best practices'
                [open] Fetch the content of the top 3 search results
                [open] Identify common advice across the top 3 results and compile them into a numbered list
[decision]      TOOL_CALL: fetch_url({"url": "https://realpython.com/async-io-python/"})
[INIT].... → Crawl4AI 0.8.6
[FETCH]... ↓ https://realpython.com/async-io-python/
| ✓ | ⏱: 2.97s
[SCRAPE].. ◆ https://realpython.com/async-io-python/
| ✓ | ⏱: 0.49s
[COMPLETE] ● https://realpython.com/async-io-python/
| ✓ | ⏱: 3.50s
[action]        → [artifact art:652c9c2276cddb98, see memory] preview: {
  "status": 200,
  "content_type": "text/markdown",
  "length_bytes": 78067,
 …

─── iter 4 ───
[memory.read]   3 hits
                tool_outcome: web_search → ok
                tool_outcome: fetch_url → ok
                tool_outcome: fetch_url → ok
[perception]    [done] Search for 'Python asyncio best practices'
                [open] Fetch the content of the top 3 search results
                [open] Identify common advice across the top 3 results and compile them into a numbered list
[decision]      TOOL_CALL: fetch_url({"url": "https://docs.python.org/3/library/asyncio.html"})
[INIT].... → Crawl4AI 0.8.6
[FETCH]... ↓ https://docs.python.org/3/library/asyncio.html
| ✓ | ⏱: 1.87s
[SCRAPE].. ◆ https://docs.python.org/3/library/asyncio.html
| ✓ | ⏱: 0.05s
[COMPLETE] ● https://docs.python.org/3/library/asyncio.html
| ✓ | ⏱: 1.94s
[action]        → [artifact art:633b13366b61fe83, see memory] preview: {
  "status": 200,
  "content_type": "text/markdown",
  "length_bytes": 9908,
  …

─── iter 5 ───
[memory.read]   4 hits
                tool_outcome: web_search → ok
                tool_outcome: fetch_url → ok
                tool_outcome: fetch_url → ok
[perception]    [done] Search for 'Python asyncio best practices'
                [open] Fetch the content of the top 3 search results
                [open] Identify common advice across the top 3 results and compile them into a numbered list
[decision]      ANSWER: I have successfully fetched the content for the top 3 search results regarding Python asyncio best practices, including the documentation from docs.python.org, the comprehensive guide from Real Python, and the overview from Medium. I have all the necessary inf…

─── iter 6 ───
[memory.read]   4 hits
                tool_outcome: web_search → ok
                tool_outcome: fetch_url → ok
                tool_outcome: fetch_url → ok
[perception]    [done] Search for 'Python asyncio best practices'
                [done] Fetch the content of the top 3 search results
                [open] Identify common advice across the top 3 results and compile them into a numbered list
[decision]      ANSWER: Based on the top 3 resources (Real Python, Python official documentation, and Medium guides), here is the common advice for working with Python asyncio:  1. **Use `async def` and `await` correctly:** Always use `async def` to define coroutines and `await` to c…

─── iter 7 ───
[memory.read]   4 hits
                tool_outcome: web_search → ok
                tool_outcome: fetch_url → ok
                tool_outcome: fetch_url → ok
[perception]    [done] Search for 'Python asyncio best practices'
                [done] Fetch the content of the top 3 search results
                [done] Identify common advice across the top 3 results and compile them into a numbered list

[done] all 3 goals satisfied

FINAL: Based on the top 3 resources (Real Python, Python official documentation, and Medium guides), here is the common advice for working with Python asyncio:

1. **Use `async def` and `await` correctly:** Always use `async def` to define coroutines and `await` to call them. Avoid using blocking synchronous code (like `time.sleep()` or standard I/O) inside async functions as it stops the entire event loop.
2. **Run the event loop properly:** Use `asyncio.run()` as the primary entry point for your main coroutine. It handles creating and closing the event loop and cancelling pending tasks for you.
3. **Use the appropriate primitives:** Prefer `asyncio.gather()` to run multiple tasks concurrently, `asyncio.create_task()` to schedule coroutines for background execution, and `asyncio.wait()` for more complex coordination of futures.
4. **Handle exceptions gracefully:** Because exceptions in background tasks (created via `create_task`) can easily be swallowed, always ensure you either await them or attach error handling to catch failures early.
5. **Use thread-safe approaches for CPU-bound tasks:** `asyncio` is not for CPU-bound code. If you have heavy computations, offload them to `concurrent.futures.ProcessPoolExecutor` via `loop.run_in_executor()` to avoid blocking the event loop.
6. **Limit concurrency:** Be mindful of resource usage when spawning many tasks; use `asyncio.Semaphore` to throttle or limit the number of concurrent operations to prevent overloading external resources or system limits.
```

Iters 1-4 cover search + three fetches; iter 5 emits an interim ANSWER closing the "fetch" goal; iter 6 emits the final consensus list; iter 7 confirms all goals done.

---

## Iteration counts vs. spec

| Query | Spec | Actual | 2× cap | Notes |
|-------|------|--------|--------|-------|
| A — Shannon Wikipedia        | 3    | **3**  | 6      | Decision rule 6 picks `fetch_url` directly; Perception bundles the multi-fact extraction into one goal |
| B — Tokyo activities         | 6    | **5**  | 12     | one `web_search` per source, one `fetch_url` for activity detail, one synthesis ANSWER |
| C run 1 — birthday + reminder | 4   | **3**  | 8      | `create_file` to nested path on first try; fact persisted to `api/state/memory.json` |
| C run 2 — recall              | 2   | **2**  | 4      | zero tool calls, answered from durable fact |
| D — asyncio synthesis        | 5–7  | **7**  | 10–14  | matches spec upper bound |

All five runs at or under their spec counts. The C1 → C2 durable-memory contract is demonstrably exercised across process restarts.

---

## Implementation notes — non-obvious bits

- **Decision XOR contract.** `DecisionOutput` rejects any output where both `answer` and `tool_call` are set or both are null. The loop catches the `ValueError`, writes a `scratchpad` memory item describing the failure, and continues — burning at most 1 iter on a bad LLM response. After 3 consecutive Decision failures the run bails out.
- **Force-attach safety net.** Perception scans each open goal text for synthesis keywords (`extract`, `summarise`, `compare`, `list`, `decide`, …). When matched, the loop picks the most-recent artifact **whose keywords overlap the goal's keywords** and sets `goal.attach_artifact_id` — the agent6 loop then loads the bytes (truncated to 24 KB) and prepends them to the Decision prompt, so Decision can answer from the artifact without re-calling `fetch_url`. The overlap filter prevents stale artifacts from prior runs (e.g. a Shannon Wikipedia blob) being attached to unrelated goals.
- **No `response_format`, no regex on LLM output.** The gateway validates with strict JSON Schema (rejects OpenAPI `nullable: true`), while the Gemini worker requires OpenAPI-style schemas (rejects union types like `{"type":["string","null"]}`). Going through `response_format` produces a 5xx either way. Instead Perception, Memory.remember, and Decision pull plain text and parse manually with `json.loads` + `model_validate` — Pydantic still enforces every contract. The fence-stripping helpers use plain `str.find`/`rstrip` (no regex).
- **Gemini-3 at temperature 0.0 loops on schema-constrained calls.** All three LLM-calling layers use `temperature=1.0`. A consequence is that `memory.remember` is non-deterministic — one run may classify "Remember my mom's birthday is …" as a fact, another may return `{"items": []}`. For Query C the assignment relies on the fact actually being persisted; if a single run misses it, re-run.
- **Windows + crawl4ai.** crawl4ai's Rich logger writes box-drawing chars; the child must use UTF-8 or it silently hangs mid-fetch. `api/mcp_server.py` reconfigures `sys.stdout/sys.stderr` to UTF-8 at startup, and `api/action.py` sets `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` in the spawned MCP server's env. `crawl4ai` is imported at module top so any first-import cost is paid before the FastMCP loop starts servicing requests.
- **Artifact threshold = 4 KB.** Tool payloads above this size get offloaded to `api/state/artifacts/<sha256>.{bin,json}` and the inline `ActionResult.result` becomes a short preview + `artifact_id`. Keeps the Decision prompt small even when fetching 260 KB Wikipedia pages.

---

## Deliverables checklist

- [x] **Code**: four cognitive layers + agent6 loop + schemas + MCP server, all under [api/](api/).
- [x] **All four queries pass** within 2× spec iter cap. See [Results](#results).
- [x] **Durable memory** across runs in `api/state/memory.json`; Query C run 2 demonstrates recall after a fresh process restart.
- [x] **Pydantic contracts** on every boundary; no free-form dicts; no regex on LLM output. JSON Schemas embedded above.
- [x] **LLM Gateway V3** is the substrate for every LLM call (perception/memory/decision via `auto_route`). No direct provider SDKs.
- [x] **State cleanable** with `rm -rf api/state/ api/sandbox/ api/usage.json`.
- [x] **README** with run instructions for each of the four queries, plus actual terminal output captured from a clean state.
- [x] **Perception and Decision Prompts + Validation JSON of PoP** (section above).
- [ ] **YouTube demo** — to be recorded; link will be added here. Suggested flow: clean state, run each of A → B → C1 → C2 → D end-to-end on screen, voice-over the iter shapes.
