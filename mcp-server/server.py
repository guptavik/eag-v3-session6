"""HTTP entry point for the MCP server (Python rewrite).

Exposes the meeting-intelligence MCP server on POST /mcp via the MCP
Python SDK's streamable-HTTP transport, plus a small set of custom
routes for /health, /auth/google, and /oauth/google/callback.

Stateless mode: the SDK manages session lifecycle; we treat each request
as independent — the extension's mcp-client.js handshakes once per popup
load via the `initialize` JSON-RPC method.

Tool argument names are camelCase (hoursAhead / endOfToday / …) to match
the on-the-wire shape the extension and the LLM already use; FastMCP
introspects the function signature to build the JSON schema the agent
sees, so the parameter name IS the schema key. Internal validation and
output shaping still goes through Pydantic v2 models in `models.py`.

Run with:
    uv run python server.py
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any, Literal

import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

# Load .env BEFORE importing tools so handlers see the env at import time.
load_dotenv()

import tools  # noqa: E402
from agents import registry as agent_registry, runner as agent_runner  # noqa: E402
from google_auth import generate_auth_url, handle_oauth_callback  # noqa: E402

PORT = int(os.environ.get("PORT", "3737"))

DEFAULT_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _build_transport_security() -> TransportSecuritySettings:
    """SDK default rejects any Origin header it doesn't recognize as a
    DNS-rebinding mitigation. That breaks the Chrome extension popup,
    whose origin is `chrome-extension://<extensionId>` — an opaque ID
    that varies per machine and per re-install.

    Two ways to configure:
      - MCP_ALLOWED_ORIGINS=chrome-extension://abc...,http://localhost:3737
        Comma-separated exact match (the SDK supports a trailing ":*"
        wildcard for port patterns, but not glob on the rest of the URL).
      - Unset → disable DNS-rebinding protection. Safe for this local-only
        dev tool; never deploy this configuration on a public network.
    """
    raw = os.environ.get("MCP_ALLOWED_ORIGINS", "").strip()
    if raw:
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_origins=origins,
            allowed_hosts=[f"localhost:{PORT}", f"127.0.0.1:{PORT}"],
        )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


# ---------------------------------------------------------------------------
# MCP server (FastMCP) + 5 tool registrations.
#
# Each tool returns a JSON-encoded string — FastMCP wraps it in a single
# text-content block, preserving the extension's `JSON.parse(block.text)`
# unwrap path verbatim.
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="meeting-intelligence",
    instructions=(
        "Tools for preparing for upcoming meetings: read the user's calendar, "
        "search Gmail, look up companies / people on the web, profile attendees, "
        "compute schedule statistics."
    ),
    stateless_http=True,
    json_response=False,
    transport_security=_build_transport_security(),
)


def _wrap_text(payload: Any) -> str:
    """The MCP wire protocol allows multiple content blocks, but the
    extension expects exactly one text block of JSON. Serializing here
    keeps the contract stable across the rewrite."""
    return json.dumps(payload, default=str)


@mcp.tool(
    name="getUpcomingMeetings",
    title="Get upcoming meetings",
    description=(
        "Fetches the user's upcoming meetings from their calendar within a time window. "
        "Use this first when the user asks about meetings, schedule, or wants to prepare "
        "for upcoming events."
    ),
)
async def get_upcoming_meetings_tool(
    hoursAhead: Annotated[float, Field(description="How many hours ahead to look. Default 24.")] = 24,
    endOfToday: Annotated[
        bool,
        Field(
            description=(
                "When true, fetch only meetings through the end of today in the user's local "
                "timezone. Use this instead of hoursAhead when the user asks about 'today'."
            ),
        ),
    ] = False,
    userTimeZone: Annotated[
        str | None,
        Field(
            description=(
                "IANA timezone name (e.g. 'America/Chicago'). Auto-injected by the MCP client — "
                "do not set this yourself."
            ),
        ),
    ] = None,
) -> str:
    payload = await tools.get_upcoming_meetings(
        {"hoursAhead": hoursAhead, "endOfToday": endOfToday, "userTimeZone": userTimeZone}
    )
    return _wrap_text(payload)


@mcp.tool(
    name="searchGmail",
    title="Search Gmail",
    description=(
        "Searches the user's email for messages matching a query. "
        "Use this to find prior context (threads, attachments, prior commitments) "
        "about a meeting, person, or company."
    ),
)
async def search_gmail_tool(
    query: Annotated[
        str, Field(description="Free-text search keywords (e.g. company name, person name, project).")
    ],
    maxResults: Annotated[
        int, Field(description="Max number of email snippets to return. Default 5.", ge=1, le=20)
    ] = 5,
    userTimeZone: Annotated[str | None, Field(description="Auto-injected; ignored by this tool.")] = None,
) -> str:
    # `userTimeZone` is accepted because mcp-client.js auto-injects it into every
    # tool call. This tool doesn't consume it — declared only to satisfy the schema.
    del userTimeZone
    payload = await tools.search_gmail({"query": query, "maxResults": maxResults})
    return _wrap_text(payload)


@mcp.tool(
    name="searchWebInfo",
    title="Search web for company/person info",
    description=(
        "Searches the web for public information about a company or a person. "
        "Use this to gather background, recent news, funding, or product context "
        "that is NOT in the user's email/calendar."
    ),
)
async def search_web_info_tool(
    query: Annotated[str, Field(description="What to search for (e.g. company name).")],
    type: Annotated[
        Literal["company", "person"],
        Field(description="Whether the query targets a company or a person."),
    ],
    userTimeZone: Annotated[str | None, Field(description="Auto-injected; ignored by this tool.")] = None,
) -> str:
    del userTimeZone  # auto-injected by client; not consumed by this tool
    payload = await tools.search_web_info({"query": query, "type": type})
    return _wrap_text(payload)


@mcp.tool(
    name="analyzeAttendeeBackground",
    title="Analyze attendee background",
    description=(
        "Looks up the professional background of a meeting attendee "
        "(role, company, work history). Use this once you know who is attending "
        "a meeting and want a quick profile."
    ),
)
async def analyze_attendee_background_tool(
    email: Annotated[str, Field(description="Email address of the attendee.")],
    userTimeZone: Annotated[str | None, Field(description="Auto-injected; ignored by this tool.")] = None,
) -> str:
    del userTimeZone  # auto-injected by client; not consumed by this tool
    payload = await tools.analyze_attendee_background({"email": email})
    return _wrap_text(payload)


@mcp.tool(
    name="calculateMeetingStats",
    title="Calculate meeting statistics",
    description=(
        "Computes statistics over a meeting set (total count, total hours, busiest day, "
        "distribution). PREFERRED USAGE for 'meeting load' / schedule-analysis queries: "
        "pass only `hoursAhead` (24 for today, 168 for a week, 720 for a month) and the "
        "tool fetches the calendar itself — no need to first call getUpcomingMeetings and "
        "re-pass the meetings array. ALTERNATIVE: pass an explicit `meetings` array for "
        "stats over a curated subset."
    ),
)
async def calculate_meeting_stats_tool(
    hoursAhead: Annotated[
        float | None,
        Field(
            description=(
                "Time window to fetch meetings for, in hours. Used when `meetings` is not provided. "
                "Defaults: today=24, week=168, month=720, otherwise 168."
            ),
        ),
    ] = None,
    timeframe: Annotated[
        Literal["today", "week", "month"] | None,
        Field(
            description=(
                "Human-readable label; also picks the default hoursAhead when neither is set."
            )
        ),
    ] = None,
    meetings: Annotated[
        list[dict[str, Any]] | None,
        Field(
            description=(
                "Optional explicit meeting set. Each item must have startTime + endTime as ISO "
                "strings (e.g. 2026-05-03T14:00:00-05:00). Only use this when stats over a "
                "curated subset are needed; for whole-week/month queries pass hoursAhead instead "
                "so the array doesn't have to be re-serialized through the LLM."
            ),
        ),
    ] = None,
    userTimeZone: Annotated[
        str | None,
        Field(
            description=(
                "IANA timezone name (e.g. 'America/Chicago') used to compute day-of-week. "
                "Auto-injected by the MCP client from the user's browser — do not set this yourself."
            ),
        ),
    ] = None,
) -> str:
    payload = await tools.calculate_meeting_stats(
        {
            "hoursAhead": hoursAhead,
            "timeframe": timeframe,
            "meetings": meetings,
            "userTimeZone": userTimeZone,
        }
    )
    return _wrap_text(payload)


# ---------------------------------------------------------------------------
# Custom HTTP routes mounted alongside the MCP transport: /health and the
# Google OAuth pair.
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "name": "meeting-intelligence-mcp", "port": PORT})


@mcp.custom_route("/auth/google", methods=["GET"])
async def auth_google(request: Request) -> RedirectResponse | HTMLResponse:
    scope_param = request.query_params.get("scope")
    scopes = scope_param.split() if scope_param else DEFAULT_GOOGLE_SCOPES
    try:
        url = generate_auth_url(scopes)
    except Exception as exc:
        return HTMLResponse(_html_page("Configuration error", str(exc)), status_code=500)
    return RedirectResponse(url, status_code=302)


_agents_ready = False
_agents_prepare_lock: Any = None  # asyncio.Lock created lazily to avoid loop-binding issues


async def _ensure_agents_ready() -> None:
    """Lazy initialization of the agent registry. Runs once per process —
    after all @mcp.tool decorators have registered their tools, so
    mcp.list_tools() returns the full set."""
    global _agents_ready, _agents_prepare_lock
    if _agents_ready:
        return
    if _agents_prepare_lock is None:
        import asyncio

        _agents_prepare_lock = asyncio.Lock()
    async with _agents_prepare_lock:
        if _agents_ready:
            return
        await agent_registry.prepare(mcp)
        _agents_ready = True


@mcp.custom_route("/agents/run", methods=["POST", "OPTIONS"])
async def agents_run(request: Request) -> StreamingResponse | JSONResponse:
    """Run the multi-agent system on a user query and stream the
    reasoning chain back as Server-Sent Events.

    Request body: {"query": str, "userTimeZone": str | null}
    Response:     text/event-stream with named events (step,
                  assistant_text, final_text, done, error).
    """
    if request.method == "OPTIONS":
        return JSONResponse({}, status_code=204)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    query = (body or {}).get("query") or ""
    if not isinstance(query, str) or not query.strip():
        return JSONResponse({"error": "Missing 'query' string"}, status_code=400)

    user_tz = (body or {}).get("userTimeZone")
    if user_tz is not None and not isinstance(user_tz, str):
        return JSONResponse({"error": "'userTimeZone' must be a string or null"}, status_code=400)

    await _ensure_agents_ready()

    return StreamingResponse(
        agent_runner.stream_agent_run(query.strip(), user_time_zone=user_tz),
        media_type="text/event-stream",
        headers={
            # Required for SSE through some proxies; also tells nginx not
            # to buffer the response.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@mcp.custom_route("/oauth/google/callback", methods=["GET"])
async def oauth_callback(request: Request) -> HTMLResponse:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(_html_page("Authorization failed", str(error)), status_code=400)
    if not code:
        return HTMLResponse(
            _html_page("Authorization failed", "Missing authorization code."), status_code=400
        )
    try:
        await handle_oauth_callback(code, state=state)
    except Exception as exc:
        print(f"[oauth] callback error: {exc}", flush=True)
        return HTMLResponse(_html_page("Authorization failed", str(exc)), status_code=500)
    return HTMLResponse(
        _html_page(
            "Google authorization complete",
            "You can close this tab and retry your query in the extension.",
        )
    )


def _html_page(heading: str, body: str) -> str:
    return (
        '<!doctype html><html><body style="font-family:system-ui,sans-serif;padding:40px;'
        f'max-width:560px;margin:auto;color:#202124"><h2>{heading}</h2><p>{body}</p></body></html>'
    )


# ---------------------------------------------------------------------------
# Entrypoint. FastMCP's `streamable_http_app()` returns a Starlette app
# preconfigured with the /mcp endpoint, the custom routes above, and the
# session-manager lifespan. We serve it under uvicorn so we can pick the
# port and respect SIGINT cleanly.
# ---------------------------------------------------------------------------


def main() -> None:
    app = mcp.streamable_http_app()

    # The Chrome extension popup runs at chrome-extension://<id> and
    # calls http://localhost:3737 from JavaScript — those are different
    # origins, so the browser enforces CORS preflight on every POST.
    # We allow any origin (the extension's manifest host_permissions are
    # the real access control) and expose Mcp-Session-Id so the SDK's
    # stateful path would work if we ever enable it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", "Mcp-Session-Id"],
        expose_headers=["Mcp-Session-Id"],
    )

    print("[mcp] meeting-intelligence service listening", flush=True)
    print(f"[mcp]   MCP tools:    http://localhost:{PORT}/mcp", flush=True)
    print(f"[mcp]   Agent runner: http://localhost:{PORT}/agents/run  (SSE)", flush=True)
    print(f"[mcp]   Health:       http://localhost:{PORT}/health", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
