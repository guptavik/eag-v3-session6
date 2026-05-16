"""Multi-agent runtime for the meeting-intelligence service.

This package mirrors the architecture diagram (orchestrator + two
specialist sub-agents) but runs server-side in Python. The Chrome
extension is a thin UI that posts a query to /agents/run and reads
back SSE events; all reasoning, tool-use translation, and Gemini
calls happen here.

Layout:
    agents/
        memory.py        AgentMemory — bounded {task → summary} history + facts
        sub_agent.py     Generic SubAgent class (own LLM loop, own tools, own memory)
        llm.py           Multi-turn Gemini wrapper with tool-use translation
        registry.py      Builds the agent registry from FastMCP's tool list
        runner.py        Async-generator entry point that emits SSE events
        main/            Orchestrator agent (delegate + calculateMeetingStats)
        workspace/       Workspace specialist (getUpcomingMeetings + searchGmail)
        research/        Research specialist (analyzeAttendeeBackground + searchWebInfo)
"""
