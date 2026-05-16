# **Meeting Intelligence Agent - Chrome Extension**
## **Complete Technical Specification**

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

### **Multi-Step Reasoning Flow**

Worked example for *"Prepare me for my next meeting"* — typically 3 LLM turns with a batch of parallel tool calls in the middle.

```
┌─────────────────────────────────────────────────────────────┐
│ User Input: "Prepare me for my next meeting"                │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ Turn 1: agent.js → Gemini API                               │
│ Sent: user query + 5 tool definitions + system prompt       │
│ Returned: 1 tool_use → getUpcomingMeetings({hoursAhead:24}) │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ Local exec: getUpcomingMeetings(24)                         │
│ Returns: [Meeting with Acme Corp at 2 PM, ...]              │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ Turn 2: agent.js → Gemini API                               │
│ Sent: full history + meeting list                           │
│ Returned: 4 tool_use blocks (parallel):                     │
│   - analyzeAttendeeBackground("john@acme.com")              │
│   - analyzeAttendeeBackground("jane@acme.com")              │
│   - searchWebInfo("Acme Corp", type: "company")             │
│   - searchGmail("Acme")                                     │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ Local exec (sequential, in returned order):                 │
│ Each tool runs; on error, retry once before surfacing       │
│ All 4 results pushed into history as one user turn          │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ Turn 3: agent.js → Gemini API                               │
│ Sent: full history + all 4 tool results                     │
│ Returned: final markdown brief, no tool_use blocks          │
│ (stop_reason = end_turn → loop exits)                       │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│ popup.js renders: collapsible reasoning chain +             │
│ structured brief (hero + attendees + talking points + ...)  │
└─────────────────────────────────────────────────────────────┘
```

### **Key Architecture Points**
1. **Conversation History**: Each LLM call includes ALL previous messages + tool results — the API is stateless.
2. **Parallel Tool Calls**: A single LLM turn can request multiple tool calls; the harness executes them sequentially in returned order, then sends all results back as one user turn.
3. **Iterative Refinement**: Agent continues until the model returns no tool_use blocks (`stop_reason: "end_turn"`). A 10-iteration safety cap prevents runaways.
4. **Per-step Retry**: Failed tool calls retry once silently; persistent failures surface as `is_error: true` tool results so the model can adapt.
5. **Visible Reasoning**: The UI streams every step (tool name, inputs, results) in real-time as collapsible rows.

---

## **5. UI/UX Design**

### **Extension Popup Layout**
```
┌──────────────────────────────────────────────────┐
│  🤖 Meeting Intelligence Agent                   │
├──────────────────────────────────────────────────┤
│                                                  │
│  API Key: [●●●●●●●●●●●●●●●●●●●●] [Save]         │
│                                                  │
│  ┌────────────────────────────────────────────┐ │
│  │  What would you like help with?            │ │
│  │                                            │ │
│  │  [Prepare for next meeting]               │ │
│  │  [Show all meetings today]                │ │
│  │  [Calculate meeting stats]                │ │
│  │                                            │ │
│  │  Or type custom query:                    │ │
│  │  [________________________________]  [Go]  │ │
│  └────────────────────────────────────────────┘ │
│                                                  │
│  ═══════════════════════════════════════════    │
│                                                  │
│  🔄 Agent Reasoning Chain:                      │
│                                                  │
│  ▼ Step 1: Fetching upcoming meetings           │
│    Tool: getUpcomingMeetings(24)               │
│    Input: { hoursAhead: 24 }                   │
│    ✓ Result: Found 1 meeting                    │
│    └─ "Product Demo with Acme Corp - 2 PM"     │
│                                                  │
│  ▼ Step 2: Researching attendees                │
│    Tool: analyzeAttendeeBackground()           │
│    Input: { name: "John Doe", email: "..." }   │
│    ⏳ Loading...                                │
│                                                  │
│  ⏸ Step 3: Pending...                           │
│                                                  │
│  ═══════════════════════════════════════════    │
│                                                  │
│  📄 Final Meeting Brief:                        │
│  [Will appear after agent completes all steps] │
│                                                  │
└──────────────────────────────────────────────────┘
```

### **UI Components**

1. **API Key Input** (persistent storage)
2. **Quick Action Buttons** (pre-defined queries)
3. **Custom Query Input** (free-form text)
4. **Reasoning Chain Display**:
   - Each step is collapsible
   - Shows: Step number, tool name, inputs, outputs
   - Real-time status: ⏳ Loading, ✓ Success, ❌ Error
5. **Final Result Section** (highlighted summary)

---

## **6. Technical Stack**

### **Frontend**
- Pure HTML/CSS/JavaScript (no frameworks)
- Chrome Extension Manifest V3
- LocalStorage for API key persistence

### **Backend/API**
- Google Gemini Generative Language API (`gemini-2.5-flash`)
- Tool calling via function declarations
- Conversation history management
- Provider-agnostic agent loop: `api.js` translates Anthropic-style messages (text / tool_use / tool_result blocks) to Gemini's `contents` / `parts` / `functionCall` / `functionResponse` shape at the API boundary, so swapping providers is a single-file change

### **External APIs**
- Mock Calendar Data (hardcoded JSON)
- Mock Gmail Data (hardcoded JSON)
- Real Web Search (optional: DuckDuckGo, Google Custom Search)

---

## **7. File Structure**

```
meeting-intelligence-agent/
├── manifest.json           # Chrome extension config
├── popup.html              # Main UI
├── popup.js                # UI logic & event handlers
├── agent.js                # Core agent logic
├── tools.js                # Tool definitions & implementations
├── api.js                  # Gemini API wrapper + Anthropic↔Gemini translation
├── styles.css              # Styling
├── mockData.js             # Mock calendar/email data
└── icons/
    ├── icon16.png
    ├── icon48.png
    └── icon128.png
```

---

## **8. Core Code Structure**

### **tools.js - Tool Definitions**
```javascript
const TOOLS = [
  {
    name: "getUpcomingMeetings",
    description: "Fetches upcoming meetings from calendar",
    input_schema: {
      type: "object",
      properties: {
        hoursAhead: { type: "number", description: "Hours to look ahead" }
      },
      required: []
    }
  },
  // ... 4 more tools
];

async function executeTool(toolName, toolInput) {
  switch(toolName) {
    case "getUpcomingMeetings":
      return await getUpcomingMeetings(toolInput);
    case "searchGmail":
      return await searchGmail(toolInput);
    // ... etc
  }
}
```

### **agent.js - Main Agent Loop**
```javascript
async function runAgent(userQuery, apiKey) {
  const conversationHistory = [
    { role: "user", content: userQuery }
  ];
  
  let agentRunning = true;
  let stepNumber = 1;
  
  while (agentRunning) {
    // Call the LLM with full history + tools
    const response = await callLLM(conversationHistory, TOOLS, apiKey);
    
    // Check if the model wants to use a tool
    if (response.stop_reason === "tool_use") {
      for (const contentBlock of response.content) {
        if (contentBlock.type === "tool_use") {
          // Display tool call in UI
          displayStep(stepNumber, contentBlock.name, contentBlock.input, "loading");
          
          // Execute the tool
          const toolResult = await executeTool(contentBlock.name, contentBlock.input);
          
          // Update UI with result
          displayStep(stepNumber, contentBlock.name, contentBlock.input, "success", toolResult);
          
          // Add tool result to conversation
          conversationHistory.push({
            role: "assistant",
            content: response.content
          });
          conversationHistory.push({
            role: "user",
            content: [{
              type: "tool_result",
              tool_use_id: contentBlock.id,
              content: JSON.stringify(toolResult)
            }]
          });
          
          stepNumber++;
        }
      }
    } else {
      // Agent is done - display final answer
      const finalAnswer = response.content.find(b => b.type === "text")?.text;
      displayFinalResult(finalAnswer);
      agentRunning = false;
    }
  }
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
