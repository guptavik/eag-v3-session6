# Meeting Intelligence MCP Server (Python rewrite)

Local MCP server that exposes the 5 meeting-intelligence tools used by the Chrome extension. Speaks streamable-HTTP MCP on `POST /mcp`.

**Session 5 change:** this server was rewritten from Node.js to **Python 3.12 + Pydantic v2** using the official MCP Python SDK. Dependency and environment management is via **[uv](https://docs.astral.sh/uv/)**. The HTTP contract over `/mcp` is unchanged, so the Chrome extension does not need to change.

## Run

```sh
cd mcp-server

# 1. uv handles Python + venv + deps in one go
uv sync

# 2. configure secrets
cp .env.example .env
#    then fill in SERPAPI_API_KEY, GEMINI_API_KEY,
#    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OWN_COMPANY_DOMAIN

# 3. start the server
uv run python server.py
```

Listens on `http://localhost:3737`. Health check: `curl http://localhost:3737/health`.

Install uv if you don't already have it:

```sh
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`uv sync` reads [pyproject.toml](pyproject.toml) and [.python-version](.python-version) (pinned at 3.12), creates a managed virtualenv under `.venv/`, and installs every dependency at a resolved, locked version. No global Python pollution.

## Configure (.env)

Copy `.env.example` to `.env` and fill in:

- **`SERPAPI_API_KEY`** — required for `searchWebInfo` and `analyzeAttendeeBackground` (when they actually need to hit the web — see [tiering](#serpapi--gemini-tiering) below). Sign up at [serpapi.com](https://serpapi.com), the Free plan gives 100 searches/month and 250/hour.

- **`GEMINI_API_KEY`** (or `GOOGLE_API_KEY`) — required for the Gemini-first path on `searchWebInfo` and the profile synthesis in `analyzeAttendeeBackground`. Get a key at [aistudio.google.com](https://aistudio.google.com). The extension also uses its own copy of this key (stored in `chrome.storage.local`).

- **`OWN_COMPANY_DOMAIN`** — *optional*, comma-separated list of email domains to treat as internal (e.g. `acme.com,acme.io`). External research is skipped for matching attendees, saving SerpAPI quota. Leave blank to disable.

- **`GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`** — required for `getUpcomingMeetings` (Calendar) and `searchGmail` (Gmail).

  1. Open [Google Cloud Console](https://console.cloud.google.com) → create or pick a project.
  2. **APIs & Services → Library** → enable **both** the **Google Calendar API** and the **Gmail API**.
  3. **APIs & Services → OAuth consent screen** → choose **External** → fill in app name + your email → add your own Google account as a **test user**.
  4. **APIs & Services → Credentials → Create credentials → OAuth client ID** → choose **Desktop**. Copy the client ID + client secret into `.env`.

  No redirect URI registration needed — Desktop OAuth clients accept any loopback (`http://localhost:*`).

Without keys, the affected tools surface a clear error to the agent; the rest still work.

## First-time Google authorization

The first time a Google-backed tool is called:

1. The server auto-opens a browser to Google's consent screen (or prints the URL if it can't).
2. You consent → Google redirects to `http://localhost:3737/oauth/google/callback`.
3. Server stores the refresh token at `~/.meeting-intel-mcp/google-tokens.json` (chmod 600 on POSIX).
4. Retry your query in the extension — subsequent calls auto-refresh access tokens silently.

Each tool requests only the scope it needs (`calendar.readonly` or `gmail.readonly`). If `searchGmail` runs after only Calendar has been authorized, the server triggers a re-auth that **merges** stored + requested scopes — so you keep Calendar access and add Gmail in one trip.

To pre-authorize everything in one click before triggering the agent: visit `http://localhost:3737/auth/google` directly.

To re-authorize from scratch (different Google account, etc.): delete `~/.meeting-intel-mcp/google-tokens.json` and call the tool again.

> **Migrating from the old Node server?** The token-file format is intentionally compatible (same path, same JSON keys: `access_token`, `refresh_token`, `scope`, `token_type`, `expiry_date`), so existing users do **not** have to re-authorize when switching to the Python version. The Python server reads the file in place.

## Tools

| Name | Backend | Notes |
|---|---|---|
| `getUpcomingMeetings` | **Google Calendar** | reads `primary` calendar, recurrences expanded, cancelled events dropped, user themselves removed from attendees, `endOfToday` support |
| `searchGmail` | **Gmail** | accepts native Gmail query syntax; returns subject / from / date / snippet for up to 20 hits |
| `searchWebInfo` | **Gemini → SerpAPI** | tiered (see below) |
| `analyzeAttendeeBackground` | **SerpAPI + Gemini** | tiered (see below); 0 API calls for internal attendees |
| `calculateMeetingStats` | real computation | accepts `hoursAhead` (preferred) or an explicit `meetings` array |

Every tool input and output is validated against a **Pydantic v2** model defined in [models.py](models.py) — the input schema the agent sees is generated directly from the tool's type-annotated parameters; the output is built from `Meeting`, `MeetingStats`, `AttendeeProfile`, etc. and serialized with `model_dump(by_alias=True)` so camelCase JSON keys match what the Chrome extension expects.

## SerpAPI / Gemini tiering

SerpAPI's free tier is 100 searches/month, so the server uses Gemini wherever Gemini is competent and only spends SerpAPI quota where it actually helps.

**`searchWebInfo`:**

- Query mentions *news / recent / latest / today / current / funding / 2026+ / etc.* → **SerpAPI directly** (Gemini's knowledge cutoff makes it useless for fresh data).
- Otherwise → **Gemini first**. If Gemini knows the entity, return its structured answer. If it returns `_unknown` or the call fails, **fall back to SerpAPI**.

**`analyzeAttendeeBackground`:**

- Email domain in `OWN_COMPANY_DOMAIN` → **0 API calls**, return an "internal teammate" stub.
- Otherwise → **1 SerpAPI call** (the only reliable source for the LinkedIn URL — Gemini hallucinates URLs) + **1 Gemini call** to synthesize `currentRole` and `background` from the SerpAPI snippets.

**LRU cache** ([cache.py](cache.py)): every external lookup is keyed by `(tool, args)` and cached in-process for the lifetime of the server. The Python version also adds in-flight dedupe — if two concurrent calls for the same key arrive (typical for the agent's parallel attendee lookups), the second one awaits the first's result instead of triggering a duplicate API call. Cap: 50 entries.

## Files

- [pyproject.toml](pyproject.toml) — uv-managed dependency manifest. Python 3.11+; pinned at 3.12 via [.python-version](.python-version).
- [server.py](server.py) — FastMCP server with 5 `@mcp.tool`-decorated functions + `@mcp.custom_route` handlers for `/health` and the OAuth pair. Uvicorn entrypoint. Loads `.env` before importing tool modules so handlers see env at import time.
- [tools.py](tools.py) — async tool implementations. Each one validates input through a Pydantic model and returns a dict shaped from output Pydantic models. Google API blocking calls run on a worker thread via `asyncio.to_thread`.
- [models.py](models.py) — **Pydantic v2** models for every tool's input and output (`Meeting`, `MeetingStats`, `AttendeeProfile`, `EmailHit`, `WebInfoResult`, …). Strict on outputs (`extra="forbid"`), lax on inputs (`extra="ignore"`) so the auto-injected `userTimeZone` from the client doesn't trip validation.
- [google_auth.py](google_auth.py) — OAuth2 flow using `google-auth-oauthlib`. Token JSON shape is identical to the Node version so existing users don't re-auth.
- [serpapi.py](serpapi.py) — async SerpAPI client (httpx). Returns a Pydantic `SerpResult`.
- [llm.py](llm.py) — async Gemini wrapper for server-side reasoning calls (`gemini_ask_json`, JSON-mode, low temperature).
- [cache.py](cache.py) — process-local LRU with in-flight dedupe (a small refinement over the Node version, which couldn't easily express it in single-threaded JS).
- [.env.example](.env.example) — template for required environment variables.

## Quick test

```sh
# initialize
curl -s -X POST http://localhost:3737/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'

# list tools
curl -s -X POST http://localhost:3737/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

# call a tool
curl -s -X POST http://localhost:3737/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"getUpcomingMeetings","arguments":{"hoursAhead":24}}}'
```

## Why Python + Pydantic v2 + uv?

- **Pydantic v2** is the canonical Python schema-validation library and the natural Python equivalent of the Node version's Zod schemas. Field-level descriptions appear directly in the JSON schema the agent sees over `tools/list`, so the LLM's tool-use guidance lives next to the type definitions instead of in a separate file.
- **uv** replaces pip + venv + pip-tools + (sometimes) pyenv with one fast tool. `uv sync` is reproducible across machines, the lockfile pins exact versions, and there's no `requirements.txt` drift.
- **MCP Python SDK** (`mcp` package) has first-class support for the streamable-HTTP transport and an ergonomic `FastMCP` API that builds tool schemas from type-annotated functions — no separate schema file to keep in sync.

## Legacy Node files

The original Node implementation (`index.js`, `server.js`, `handlers.js`, `google-auth.js`, `serpapi.js`, `llm.js`, `cache.js`, `package.json`, `package-lock.json`) is still present on disk for diff/review during the migration. Run `git log --oneline mcp-server/` if you need to see what changed. They can be deleted once you've confirmed the Python server works end-to-end.
