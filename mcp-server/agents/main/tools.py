"""Main / orchestrator agent's owned-tool declarations.

MAIN_DIRECT_TOOL_NAMES is the subset of real MCP tools the
orchestrator calls directly (no sub-agent in between). Currently
just `calculateMeetingStats` — a pure compute the orchestrator uses
to compose meeting-load briefs without round-tripping a `meetings`
array through a sub-agent.

DELEGATE_TOOL is the synthetic in-process tool the orchestrator
uses to hand sub-tasks to the workspace and research sub-agents.
It is NOT an MCP tool — see agents/main/agent.py for the
`tool_handlers["delegate"]` implementation that resolves it locally.
"""

from __future__ import annotations

MAIN_DIRECT_TOOL_NAMES = frozenset({"calculateMeetingStats"})

DELEGATE_TOOL = {
    "name": "delegate",
    "description": (
        "Hand a sub-task to one of the specialist sub-agents. Each sub-agent "
        "runs its own short reasoning loop with its own tools and memory, then "
        "returns a synthesized result. Use this whenever you need raw data — "
        "do NOT try to fetch calendar, email, web, or attendee data yourself.\n\n"
        "Sub-agents:\n"
        "  - 'workspace': Google Calendar + Gmail. Use for fetching upcoming meetings, "
        "searching email threads.\n"
        "  - 'research':  Web search + attendee profiles. Use for company background, "
        "LinkedIn URLs, attendee bios.\n\n"
        "You can issue multiple delegate() calls in the same turn so the sub-agents "
        "run in parallel."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "enum": ["workspace", "research"],
                "description": (
                    "Which sub-agent to call. 'workspace' = Google Workspace tools "
                    "(calendar, gmail). 'research' = external web/profile lookups."
                ),
            },
            "task": {
                "type": "string",
                "description": (
                    "Plain-language task for the sub-agent. Include any context it "
                    "needs: which meeting, which attendees, what date range, what to "
                    "summarize. Sub-agent will return a short structured summary, not "
                    "a final brief."
                ),
            },
        },
        "required": ["agent", "task"],
    },
}
