"""Workspace agent's owned-tool name set.

Listing tool names here (rather than in agents/registry.py) keeps
each agent's interface self-contained — to add a new MCP tool to
the workspace agent, you only edit this file. agents/registry.py
reads this set when partitioning the MCP tool list into the
per-agent subsets.
"""

WORKSPACE_TOOL_NAMES = frozenset({"getUpcomingMeetings", "searchGmail"})
