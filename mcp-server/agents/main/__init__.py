"""Orchestrator (main) agent — plans, delegates, synthesizes the brief.

Owned tools: `delegate` (synthetic, in-process) + `calculateMeetingStats`
(real MCP tool). The orchestrator has no direct access to calendar /
email / web / attendee tools — those live on the workspace and
research sub-agents.
"""
