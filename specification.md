# **Meeting Intelligence Agent - Chrome Extension**
## **Complete Technical Specification**

---

> **Session 6 status.** This document originated as the Session 1 build spec (single-agent loop in JavaScript, mock data, all tools in the extension). It has since evolved through five sessions — most importantly Session 6, which moved the agent runtime out of the extension into a Python service. The high-level intent (Sections 1–3, 9, 11) is unchanged. The architecture-specific sections below (4, 6, 7, 8) have been updated to describe the current Session-6 design. **For the canonical, up-to-date description, see [README.md](README.md).**

---

## **1. Overview**

### **Purpose**
An agentic AI Chrome extension that prepares users for upcoming meetings by autonomously gathering context through multi-step reasoning and external tool calls.

### **Core Value Proposition**
- LLM alone: ❌ Cannot access calendar, emails, or external data
- Agent with tools: ✓ Fetches real data, performs analysis, generates actionable briefs

### **Assignment Alignment**
- ✅ Multi-step LLM calls with full conversation history
- ✅ External tool/API calls (calendar, email, web search)
- ✅ Tasks LLM cannot do alone (data fetching, calculations)
- ✅ Visible reasoning chain in UI
- ✅ 5+ custom tools

---

## **2. User Stories**

### **Primary Use Case**
```
User clicks: "Prepare for my next meeting"

Agent executes:
1. Fetches upcoming meetings from calendar
2. Researches each attendee's background
3. Searches company information
4. Finds related email threads
5. Calculates meeting statistics
6. Generates comprehensive meeting brief

User sees: Step-by-step reasoning + final brief
```

### **Example Queries**
1. *"Prepare me for my next meeting"*
2. *"Show me all meetings today and research the attendees"*
3. *"What's my meeting load this week?"*
4. *"Find context about my 2 PM meeting with Acme Corp"*

---

## **3. Custom Tools (Minimum 5)**

### **Tool 1: `getUpcomingMeetings`**
```javascript
{
  name: "getUpcomingMeetings",
  description: "Fetches upcoming meetings from user's calendar",
  parameters: {
    hoursAhead: "number (default: 24) - How many hours to look ahead"
  },
  returns: [
    {
      id: "meeting_123",
      title: "Product Demo with Acme Corp",
      startTime: "2024-01-15T14:00:00Z",
      endTime: "2024-01-15T15:00:00Z",
      attendees: ["john.doe@acme.com", "jane.smith@acme.com"],
      location: "Zoom",
      description: "Discuss Q1 roadmap and pricing"
    }
  ]
}
```

**Implementation**: Mock data (to avoid OAuth complexity)

---

### **Tool 2: `searchGmail`**
```javascript
{
  name: "searchGmail",
  description: "Searches user's email for relevant context",
  parameters: {
    query: "string - Search keywords (e.g., 'Acme Corp product demo')",
    maxResults: "number (default: 5)"
  },
  returns: [
    {
      subject: "Re: Product Demo Preparation",
      from: "john.doe@acme.com",
      date: "2024-01-10",
      snippet: "Looking forward to discussing pricing tiers..."
    }
  ]
}
```

**Implementation**: Mock email database

---

### **Tool 3: `searchWebInfo`**
```javascript
{
  name: "searchWebInfo",
  description: "Searches the web for information about companies or people",
  parameters: {
    query: "string - What to search for",
    type: "string - 'company' or 'person'"
  },
  returns: [
    {
      title: "Acme Corp - Company Profile",
      snippet: "B2B SaaS company, 500 employees, recently raised $50M...",
      url: "https://acme.com/about"
    }
  ]
}
```

**Implementation**: Real web search (DuckDuckGo API or scraping)

---

### **Tool 4: `analyzeAttendeeBackground`**
```javascript
{
  name: "analyzeAttendeeBackground",
  description: "Researches professional background of meeting attendees",
  parameters: {
    name: "string - Person's name",
    email: "string - Email address",
    company: "string - Company name"
  },
  returns: {
    name: "John Doe",
    currentRole: "VP of Engineering",
    company: "Acme Corp",
    background: "10 years at Acme, previously at Google Cloud",
    linkedInUrl: "https://linkedin.com/in/johndoe"
  }
}
```

**Implementation**: Mock data + optional web search

---

### **Tool 5: `calculateMeetingStats`**
```javascript
{
  name: "calculateMeetingStats",
  description: "Calculates statistics about meeting schedule",
  parameters: {
    meetings: "array - List of meeting objects",
    timeframe: "string - 'today', 'week', 'month'"
  },
  returns: {
    totalMeetings: 12,
    totalHours: 18.5,
    averageDuration: 1.54,
    busiestDay: "Wednesday",
    meetingDistribution: {
      "Monday": 2,
      "Tuesday": 3,
      "Wednesday": 5,
      "Thursday": 1,
      "Friday": 1
    }
  }
}
```

**Implementation**: Real calculation logic

---

### **Bonus Tool 6: `generateMeetingBrief`**
```javascript
{
  name: "generateMeetingBrief",
  description: "Synthesizes all gathered information into actionable brief",
  parameters: {
    meetingData: "object - All collected meeting context"
  },
  returns: {
    summary: "Meeting with Acme Corp at 2 PM...",
    attendees: [...],
    companyContext: "...",
    emailContext: "...",
    talkingPoints: ["Discuss pricing", "Address Q1 concerns"],
    preparationTips: ["Review pricing deck", "Prepare demo environment"]
  }
}
```

**Implementation**: LLM synthesis of all tool results

---

## **4. Agent Flow Architecture**

### **Multi-Agent System**

The agent layer is now three cooperating agents — one orchestrator plus two specialist sub-agents — each with its own system prompt, its own Gemini loop, and its own memory. The orchestrator handles user-facing planning and final brief synthesis; the sub-agents own the actual tool calls.

```
┌─────────────────┐    ┌───────────────┐
│ Memory (main)   │    │ Tools (main)  │
│ history + facts │    │ delegate +    │
│                 │    │ stats         │
└────────┬────────┘    └───────┬───────┘
         └──────────┬──────────┘
                    │
   ┌────────────────▼────────────────┐
   │   Orchestrator (main agent)     │   plans → delegates → synthesizes brief
   └────────┬───────────────────┬────┘
            │                   │
  ┌─────────▼───────┐  ┌────────▼────────┐
  │ Workspace agent │  │ Research agent  │
  │ Memory + Tools  │  │ Memory + Tools  │
  │ getUpcoming…    │  │ analyzeAttendee │
  │ searchGmail     │  │ searchWebInfo   │
  └─────────────────┘  └─────────────────┘
```

### **Worked Example — "Prepare me for my next meeting"**

```
EXTENSION  ─POST /agents/run─►  Python service
           ◄────── SSE ───────  events stream back live

ORCHESTRATOR turn 1 → delegate(workspace, "fetch upcoming meetings, next 24h")
  └─► WORKSPACE sub-agent
        turn 1: getUpcomingMeetings({hoursAhead: 24})
        turn 2: end_turn — "Found 1 meeting: Acme Q4 Sync, Tue 2pm, attendees …"

ORCHESTRATOR turn 2 → parallel delegations:
  ├─► WORKSPACE   delegate("search email for 'Acme'")
  │     turn 1: searchGmail({query: "Acme"})
  │     turn 2: end_turn — 3-hit summary
  │
  └─► RESEARCH    delegate("profile attendees X,Y + Acme Corp")
        turn 1: parallel analyzeAttendeeBackground × 2 + searchWebInfo
        turn 2: end_turn — profile + company summary

ORCHESTRATOR turn 3 → end_turn: final markdown brief
                       SSE emits: final_text → done
                       popup.js renders attendees, talking points, prep checklist
```

### **Key Architecture Points**
1. **Three agents, one Python process.** Orchestrator + workspace + research, all built on the same `SubAgent` base class in `mcp-server/agents/sub_agent.py`. Each owns its own system prompt, Gemini loop, and `AgentMemory`.
2. **`delegate` is the only data-acquisition path the orchestrator has.** It cannot call `getUpcomingMeetings`/`searchGmail`/`analyzeAttendeeBackground`/`searchWebInfo` directly — anything needing raw data is routed through the named sub-agent.
3. **Memory is per-agent and process-scoped.** Each `SubAgent` instance is a module-scope singleton in `agents/registry.py`; its 5-entry `(task → summary)` history is prepended to its next user message, so follow-up queries reuse prior context. Server restart clears it.
4. **Tools are in-process.** Sub-agents resolve tool calls via `mcp.call_tool()` — same Pydantic validation and same `tools.py` handlers as the MCP wire transport, just no HTTP hop. The user's timezone is propagated through a `contextvars.ContextVar` set at the top of each `/agents/run` request.
5. **Per-step retry.** Failed tool calls retry once silently; persistent failures surface as `is_error: true` so the model can adapt.
6. **SSE streaming.** Events (`step`, `assistant_text`, `final_text`, `done`, `error`) flow back to the popup over a single `POST /agents/run` response. The extension renders each as a colored row tagged with the agent that produced it (main / workspace / research).
7. **MCP unchanged.** `/mcp` still speaks streamable-HTTP MCP, so another MCP host (Claude Desktop, Cursor) can use the five tools directly without involving the agent runtime.

---

## **5. UI/UX Design**

### **Extension Popup Layout**
```
┌──────────────────────────────────────────────────┐
│  🤖 Meeting Intelligence Agent                   │
├──────────────────────────────────────────────────┤
│  ┌────────────────────────────────────────────┐ │
│  │  What would you like help with?            │ │
│  │  [Prepare for next meeting]                │ │
│  │  [Show all meetings today]                 │ │
│  │  [Calculate meeting stats]                 │ │
│  │  Or type custom query:                     │ │
│  │  [________________________________] [Go]   │ │
│  └────────────────────────────────────────────┘ │
│                                                  │
│  🔄 Agent Reasoning Chain:                      │
│  ▼ Step 1 [WORKSPACE] getUpcomingMeetings()    │
│    ✓ Found 1 meeting — "Acme Q4 Sync, Tue 2pm" │
│  ▼ Step 2 [RESEARCH]  analyzeAttendeeBackground│
│    ⏳ profiling john@acme.com…                  │
│  ▼ Step 3 [RESEARCH]  searchWebInfo("Acme")   │
│    ⏳ running…                                  │
│                                                  │
│  📄 Final Meeting Brief:                        │
│  # Acme Q4 Sync                                 │
│  **When:** Tue, May 5 · 2:00 PM CST            │
│  ## Attendees ...                               │
└──────────────────────────────────────────────────┘
```

### **UI Components**

1. **No API key input.** Gemini key lives server-side in `mcp-server/.env`.
2. **Quick Action Buttons** — pre-defined queries.
3. **Custom Query Input** — free-form text.
4. **Reasoning Chain Display**:
   - Each step collapsible, tagged with the agent that produced it (`MAIN` / `WORKSPACE` / `RESEARCH`) via colored pill + matching left-border accent
   - Shows: step number, agent, tool name, inputs, outputs
   - Real-time status from SSE events: ⏳ loading → 🔄 retrying → ✓ success / ❌ error
5. **Final Brief Section** — markdown-rendered structured cards (hero, attendees, company, talking points, prep checklist).

---

## **6. Technical Stack**

### **Frontend (Chrome extension — thin UI client)**
- Pure HTML/CSS/JavaScript (no framework, no build step)
- Chrome Extension Manifest V3
- Two JS files: `agent-client.js` (POST `/agents/run` + parse SSE) and `popup.js` (render reasoning chain + brief)

### **Backend (Python service, `mcp-server/`)**
- Python 3.12 + Pydantic v2, managed by [uv](https://docs.astral.sh/uv/)
- Official MCP Python SDK (`mcp` package) on Starlette + uvicorn
- Two HTTP surfaces on the same process:
  - `POST /mcp` — streamable-HTTP MCP tool transport (unchanged from Session 5)
  - `POST /agents/run` — multi-agent runtime, returns Server-Sent Events
- Multi-agent runtime: `agents/sub_agent.py` (LLM loop), `agents/registry.py` (agent registry + tool partition), `agents/runner.py` (SSE event stream), plus per-agent folders `agents/{main,workspace,research}/` each holding `prompt.py` + `tools.py`
- Two Gemini wrappers: `agents/llm.py` (multi-turn with tool_use translation, used by agents) and `llm.py` (single-shot JSON mode, used by SerpAPI-tiering tool handlers)

### **External APIs**
- Google Gemini Generative Language API (`gemini-2.5-flash`) — for both the agent loop and server-side tool reasoning
- Google Calendar API — `getUpcomingMeetings`
- Gmail API — `searchGmail`
- SerpAPI (Google SERP) — `searchWebInfo`, `analyzeAttendeeBackground`

---

## **7. File Structure**

```
eag-v3-session6/
├── manifest.json           # Chrome extension config (MV3)
├── popup.html              # Main UI
├── popup.js                # UI controller + brief renderer
├── agent-client.js         # POST /agents/run + SSE reader; calls UI callbacks
├── styles.css              # Styling (incl. per-agent pill + border tints)
├── icons/                  # Extension icons
└── mcp-server/             # Python 3.12 + Pydantic v2, managed by uv
    ├── server.py           # FastMCP + Starlette: /mcp · /agents/run · /health · OAuth
    ├── tools.py            # 5 async MCP tool implementations
    ├── models.py           # Pydantic v2 I/O models
    ├── google_auth.py      # OAuth client + token persistence
    ├── serpapi.py          # async SerpAPI client
    ├── llm.py              # JSON-mode Gemini wrapper (for tool-side reasoning)
    ├── cache.py            # process-local LRU with in-flight dedupe
    ├── pyproject.toml      # uv-managed dependency manifest
    └── agents/             # Multi-agent runtime
        ├── memory.py       # AgentMemory: bounded history + facts
        ├── sub_agent.py    # Generic SubAgent class (LLM loop, retry, emit)
        ├── llm.py          # Multi-turn Gemini wrapper + tool-schema sanitizer
        ├── registry.py     # Tool partition + agent registry + run() entry
        ├── runner.py       # Async generator that streams events as SSE
        ├── main/           # Orchestrator
        │   ├── prompt.py
        │   └── tools.py    # DELEGATE_TOOL + MAIN_DIRECT_TOOL_NAMES
        ├── workspace/      # Google Workspace specialist
        │   ├── prompt.py
        │   └── tools.py    # WORKSPACE_TOOL_NAMES
        └── research/       # External-research specialist
            ├── prompt.py
            └── tools.py    # RESEARCH_TOOL_NAMES
```

---

## **8. Core Code Structure**

### **`agents/sub_agent.py` — generic agent loop**
```python
class SubAgent:
    def __init__(self, *, name, system_prompt, tools,
                 tool_executor, tool_handlers=None, max_iterations=None):
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools                # live ref to *_TOOLS array
        self.tool_executor = tool_executor
        self.tool_handlers = tool_handlers or {}
        self.max_iterations = max_iterations or SUB_AGENT_MAX_ITERATIONS
        self.memory = AgentMemory(name)

    async def run(self, task, emit, *, user_time_zone=None):
        history = [make_user_text_message(prepend_memory(task, self.memory))]
        for _ in range(self.max_iterations):
            response = await call_llm(history, self.tools,
                                      system_prompt=self.system_prompt,
                                      user_time_zone=user_time_zone)
            # Stream text to UI
            for tb in text_blocks(response):
                await emit({"kind": "assistant_text", "agent": self.name, "text": tb})

            history.append(model_turn(response))
            if response["stop_reason"] != "tool_use":
                self.memory.record_call(task, final_text(response))
                return {"text": final_text(response), "stop_reason": "end_turn"}

            tool_results = []
            for block in tool_use_blocks(response):
                # tool_handlers["delegate"] is in-process (orchestrator only)
                # otherwise call_executor → mcp.call_tool() → Pydantic-validated handler
                result = await self._execute_with_retry(block, emit)
                tool_results.append(result)
            history.append(make_tool_results_message(tool_results))
        return {"text": "", "stop_reason": "max_iterations"}
```

### **`agents/registry.py` — orchestrator construction per request**
```python
async def run(query, emit, *, user_time_zone=None):
    reset_step_counter()
    token = _user_tz_var.set(user_time_zone)
    try:
        sub_agents = {"workspace": _workspace_agent, "research": _research_agent}

        async def delegate_handler(args):
            sub = sub_agents[args["agent"]]
            outcome = await sub.run(args["task"], emit, user_time_zone=user_time_zone)
            return {"agent": args["agent"], "summary": outcome["text"]}

        orchestrator = SubAgent(
            name="main",
            system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
            tools=_main_tools,                # [DELEGATE_TOOL, calculateMeetingStats]
            tool_executor=_execute_mcp_tool,  # mcp.call_tool wrapper
            tool_handlers={"delegate": delegate_handler},
            max_iterations=ORCHESTRATOR_MAX_ITERATIONS,
        )
        return await orchestrator.run(query, emit, user_time_zone=user_time_zone)
    finally:
        _user_tz_var.reset(token)
```

### **`agent-client.js` — extension reads SSE, fires UI callbacks**
```javascript
async function runAgent(userQuery, { onStep, onAssistantText, onFinalText, onError }) {
  const res = await fetch("http://localhost:3737/agents/run", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify({ query: userQuery, userTimeZone: detectTz() })
  });
  const reader = res.body.getReader();
  // … parse SSE frames, dispatch on event name (step / assistant_text /
  //   final_text / done / error) and invoke the matching UI callback.
}
```

---

## **9. Assignment Compliance Checklist**

- ✅ **Multi-step LLM calls**: Agent calls the LLM 5-7 times
- ✅ **Full conversation history**: Each call includes ALL previous messages + tool results
- ✅ **External tool calls**: Calendar, Gmail, Web Search APIs
- ✅ **Visible reasoning chain**: UI displays every step with inputs/outputs
- ✅ **5+ custom tools**: getUpcomingMeetings, searchGmail, searchWebInfo, analyzeAttendeeBackground, calculateMeetingStats, generateMeetingBrief
- ✅ **Complex task**: LLM cannot access calendar/email alone
- ✅ **Agent decides flow**: LLM autonomously chooses which tools to use

---

## **10. Implementation Plan**

### **Phase 1: Setup** (15 min)
1. Create manifest.json
2. Basic HTML structure
3. Gemini API integration

### **Phase 2: Tools** (30 min)
4. Implement 5 tools with mock data
5. Tool execution logic
6. Test each tool individually

### **Phase 3: Agent Logic** (30 min)
7. Conversation history management
8. Agent loop with tool calling
9. Error handling

### **Phase 4: UI** (30 min)
10. Reasoning chain display
11. Step-by-step updates
12. Final result formatting

### **Phase 5: Polish** (15 min)
13. Styling
14. Loading states
15. Testing & debugging

**Total: ~2 hours**

---

## **11. Success Criteria**

The extension successfully demonstrates:
1. ✅ User asks: "Prepare for my next meeting"
2. ✅ Agent autonomously fetches calendar data
3. ✅ Agent researches attendees and company
4. ✅ Agent searches emails for context
5. ✅ Agent calculates meeting statistics
6. ✅ Agent synthesizes comprehensive brief
7. ✅ UI shows all 5-7 reasoning steps
8. ✅ Final brief is actionable and useful
