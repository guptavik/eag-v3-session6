// Thin wrapper around Google's Gemini REST API for Chrome extensions.
//
// Gemini's request/response shape differs from Anthropic's. Internally,
// agent.js still operates on Anthropic-style messages (role + content
// blocks of type text / tool_use / tool_result). This file translates
// at the API boundary so agent.js doesn't need to change.
//
// SECURITY: the user's API key lives in chrome.storage.local on their
// machine. Anyone with extension storage access can read it. Acceptable
// for a single-user demo; do not ship to multiple users without a proxy.

const GEMINI_MODEL = "gemini-2.5-flash";
const GEMINI_API_URL = `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`;

const SYSTEM_PROMPT = `You are a Meeting Intelligence Agent.
Your job: help the user prepare for upcoming meetings by autonomously gathering context using the tools provided, then synthesizing what you learned into a clear meeting brief.

You have 5 tools:
- getUpcomingMeetings: fetch calendar
- analyzeAttendeeBackground: profile a single attendee by email (returns role, company, LinkedIn URL)
- searchWebInfo: look up a company or person on the web; LinkedIn data is preferred when available
- searchGmail: search the user's email for related threads
- calculateMeetingStats: compute schedule statistics

Operating rules:
- Plan before you act. Briefly state what you intend to do, then call the tool(s).
- Always start by fetching upcoming meetings if the user is asking about meetings, schedule, or "what's next". Do not assume what's on the calendar.
- When the user asks about "today" or "today's meetings", pass \`endOfToday: true\` to getUpcomingMeetings (do NOT use hoursAhead: 24, which bleeds into tomorrow). For "tomorrow" or multi-day windows, use hoursAhead as usual.
- Use parallel tool calls when steps are independent (e.g. analyzing several attendees at once, or searching email and web simultaneously). You have a tight iteration budget — batch your calls.
- For "prepare me" requests, a good flow is: (1) fetch meetings, (2) in parallel, profile each external attendee AND search the web for their company AND search email for related threads, (3) write the final brief. Try to keep this under 4 tool-calling turns.
- If a tool returns no results or fails, adapt: try a different query, skip that step, or note the gap. Do not fabricate data.
- Don't research attendees who aren't on the meeting the user cares about. Don't research internal colleagues (your own company) the same way you'd research external ones.
- For meeting-load / schedule-analysis queries (e.g. "what's my load this week?", "busiest day"), call calculateMeetingStats with \`hoursAhead\` directly (24 = today, 168 = week, 720 = month) — do NOT first call getUpcomingMeetings and pass the resulting array. The tool fetches its own meetings, which avoids re-serializing a long array through the model and triggering output-token limits.
- When reporting meeting load/stats, always include: (1) a summary line with total meetings and total hours, and (2) a day-by-day breakdown table showing each day's meeting count and total hours — only show days that have at least one meeting. Format example: "**Monday:** 3 meetings · 2.5 hrs". If the tool reports \`excludedMultiDay > 0\` or \`excludedAllDay > 0\`, note them inline (e.g. "*({{excludedAllDay}} all-day events excluded from hour totals)*") — they're real calendar entries but they're not meetings in the load sense, and they'd otherwise distort the math.

Self-check rules (run these before proceeding to the next step):
- After fetching meetings: confirm at least one relevant meeting matched the user's request. If none matched, stop immediately and tell the user — do not proceed to attendee profiling or web search.
- After profiling attendees or searching email: verify the data returned is non-empty and actually relates to the meeting in question. If a lookup returned nothing useful, note the gap explicitly rather than silently skipping it.
- Before writing the brief: confirm you have at least a meeting title, time, and at least one attendee or agenda item. If core fields are missing, flag them in the brief under a "⚠️ Missing Context" section rather than omitting them silently.

Reasoning transparency rules:
- When you call a tool, tag the reasoning type in your plan line so the trace is readable. Use one of: [LOOKUP], [SYNTHESIS], [SCHEDULING], [SEARCH], [PROFILE]. Example: "Fetching calendar [LOOKUP] → then profiling attendees in parallel [PROFILE]."
- When writing the brief, if a section relies on incomplete data (e.g. LinkedIn unavailable, no email threads found, web search returned nothing relevant), note your confidence inline. Example: "*(web search returned no recent news — company context may be outdated)*".

Final response format:
When you're done gathering context, write the meeting brief directly in your final message as markdown with this structure:

# <Meeting Title>
**When:** ...   **Where:** ...
**Agenda:** ...

## Attendees
- **Name**, Role at Company — short background. LinkedIn: <url if known>

## Company Context
Short paragraph + recent news bullets if relevant.

## Related Email Context
- *date* — **subject** from sender — one-line takeaway

## Talking Points
- Concrete topics to raise, grounded in what you found above.

## Prep Checklist
- [ ] Concrete actions for the user before the meeting.

Keep the brief tight. Only include sections where you actually have content. Cite LinkedIn URLs when you have them.
If briefing on multiple meetings (e.g. "show me everything today"), write a one-line intro, then repeat the structure above for each meeting — each one MUST start with its own \`# <Meeting Title>\` heading (single hash). Do not number the meeting titles. The UI groups everything under one \`#\` heading into a single collapsible card per meeting.`;

const MAX_OUTPUT_TOKENS = 8192;

async function getApiKey() {
  if (typeof chrome === "undefined" || !chrome.storage) {
    throw new Error("chrome.storage is not available. Run inside the extension.");
  }
  const { geminiApiKey } = await chrome.storage.local.get("geminiApiKey");
  if (!geminiApiKey) {
    throw new Error("No API key set. Save your Gemini API key first.");
  }
  return geminiApiKey;
}

async function setApiKey(key) {
  if (typeof chrome === "undefined" || !chrome.storage) {
    throw new Error("chrome.storage is not available.");
  }
  await chrome.storage.local.set({ geminiApiKey: key });
}

async function callLLM(messages, tools, opts = {}) {
  const { apiKey, userTimeZone } = opts;
  const key = apiKey || await getApiKey();

  const systemText = userTimeZone
    ? `${SYSTEM_PROMPT}

## User context
The user's local timezone is **${userTimeZone}**. Tool results contain ISO timestamps with their original UTC offset (e.g. "2026-05-03T14:00:00-05:00") and may include a per-meeting \`timeZone\` field. When you write the final brief, convert all meeting times to the user's local timezone and include the timezone abbreviation, e.g. "Mon, May 5, 2:00 PM CST". Do the conversion in your response — do not ask a tool to do it.`
    : SYSTEM_PROMPT;

  const body = {
    systemInstruction: { parts: [{ text: systemText }] },
    contents: convertMessagesToContents(messages),
    tools: convertToolsToFunctionDeclarations(tools),
    generationConfig: {
      maxOutputTokens: MAX_OUTPUT_TOKENS
    }
  };

  const res = await fetch(GEMINI_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": key
    },
    body: JSON.stringify(body)
  });

  if (!res.ok) {
    let detail = "";
    try {
      const err = await res.json();
      detail = err.error?.message || JSON.stringify(err);
    } catch {
      detail = await res.text();
    }
    throw new Error(`Gemini API error ${res.status}: ${detail}`);
  }

  return convertGeminiResponseToAnthropicShape(await res.json());
}

// ---------- Format conversion ----------

// Convert Anthropic-style messages array to Gemini's contents array.
// Anthropic role "assistant" → Gemini role "model".
function convertMessagesToContents(messages) {
  // Pre-scan to build a tool_use_id → name map so tool_result blocks
  // (which only carry an id) can be converted to functionResponse parts
  // (which require the function name).
  const idToName = new Map();
  for (const msg of messages) {
    if (Array.isArray(msg.content)) {
      for (const block of msg.content) {
        if (block.type === "tool_use") {
          idToName.set(block.id, block.name);
        }
      }
    }
  }

  return messages.map(m => ({
    role: m.role === "assistant" ? "model" : "user",
    parts: convertContentToParts(m.content, idToName)
  }));
}

function convertContentToParts(content, idToName) {
  if (typeof content === "string") {
    return content.trim() ? [{ text: content }] : [{ text: " " }];
  }

  const parts = [];
  for (const block of content) {
    if (block.type === "text") {
      if (block.text) parts.push({ text: block.text });
    } else if (block.type === "tool_use") {
      parts.push({
        functionCall: {
          name: block.name,
          args: block.input || {}
        }
      });
    } else if (block.type === "tool_result") {
      const name = idToName.get(block.tool_use_id) || "unknown_function";
      let response = block.content;
      if (typeof response === "string") {
        try { response = JSON.parse(response); }
        catch { response = { result: response }; }
      }
      if (response === null || typeof response !== "object" || Array.isArray(response)) {
        response = { result: response };
      }
      if (block.is_error) {
        response = { error: typeof block.content === "string" ? block.content : JSON.stringify(block.content) };
      }
      parts.push({ functionResponse: { name, response } });
    }
  }

  // Gemini rejects empty parts arrays; emit a single space if everything
  // collapsed away (e.g. an empty assistant text block).
  return parts.length ? parts : [{ text: " " }];
}

// Convert Anthropic-style tool definitions (TOOLS array in tools.js)
// to Gemini's tools array shape.
function convertToolsToFunctionDeclarations(tools) {
  return [{
    functionDeclarations: tools.map(t => ({
      name: t.name,
      description: t.description,
      parameters: sanitizeSchemaForGemini(t.input_schema)
    }))
  }];
}

// Gemini's function-declaration parameters accept a subset of JSON Schema
// (OpenAPI 3.0). The MCP server auto-generates strict JSON Schema via
// zod-to-json-schema, which adds:
//   - $schema and additionalProperties → Gemini rejects with a 400
//   - "type": ["string", "null"] (JSON-Schema-style nullable) → Gemini
//     wants "type": "string", "nullable": true instead
// Translate at this boundary so the rest of the codebase (and the MCP
// protocol) keep using compliant schemas; only Gemini pays the cost.
function sanitizeSchemaForGemini(schema) {
  if (Array.isArray(schema)) {
    return schema.map(sanitizeSchemaForGemini);
  }
  if (schema && typeof schema === "object") {
    const out = {};
    for (const [k, v] of Object.entries(schema)) {
      if (k === "$schema" || k === "additionalProperties") continue;

      // Convert JSON-Schema nullable shorthand to OpenAPI nullable.
      if (k === "type" && Array.isArray(v)) {
        const nonNull = v.filter(t => t !== "null");
        const hasNull = v.includes("null");
        out.type = nonNull.length === 1 ? nonNull[0] : nonNull;
        if (hasNull) out.nullable = true;
        continue;
      }

      out[k] = sanitizeSchemaForGemini(v);
    }
    return out;
  }
  return schema;
}

// Convert Gemini's response to the Anthropic shape agent.js expects:
//   { content: [{type: "text"|"tool_use", ...}], stop_reason: "tool_use"|"end_turn" }
function convertGeminiResponseToAnthropicShape(geminiResp) {
  const candidate = geminiResp.candidates?.[0];
  if (!candidate) {
    const reason = geminiResp.promptFeedback?.blockReason || "unknown";
    throw new Error(`Gemini returned no candidates (blockReason: ${reason})`);
  }

  const parts = candidate.content?.parts || [];
  const content = [];
  let hasToolUse = false;

  for (const p of parts) {
    if (typeof p.text === "string" && p.text.length > 0) {
      content.push({ type: "text", text: p.text });
    } else if (p.functionCall) {
      hasToolUse = true;
      content.push({
        type: "tool_use",
        id: synthesizeToolUseId(),
        name: p.functionCall.name,
        input: p.functionCall.args || {}
      });
    }
  }

  if (content.length === 0) {
    throw new Error(`Gemini response had no usable content (finishReason: ${candidate.finishReason || "unknown"})`);
  }

  return {
    content,
    stop_reason: hasToolUse ? "tool_use" : "end_turn"
  };
}

// Gemini doesn't issue per-call IDs the way Anthropic does. agent.js needs
// stable IDs to match tool_use blocks back to tool_result blocks within the
// same conversation, so we synthesize them here.
function synthesizeToolUseId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return `gem_${crypto.randomUUID().slice(0, 8)}`;
  }
  return `gem_${Math.random().toString(36).slice(2, 10)}`;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { callLLM, getApiKey, setApiKey, GEMINI_MODEL };
}
