// Thin Chrome-extension client for the Python multi-agent service.
//
// Replaces the previous in-extension agent loop. The extension POSTs
// the user query to http://localhost:3737/agents/run; the server
// streams Server-Sent Events back as the agents progress through
// their tool-use loop. We parse the SSE frames and call the same
// callbacks the old runAgent() did, so popup.js doesn't have to
// change its rendering code.
//
// Event types (mirror agents/runner.py):
//   step           → onStep(payload)            // tool-call lifecycle
//   assistant_text → onAssistantText(text, agent)
//   final_text     → onFinalText(text)
//   error          → onError(new Error(message))
//   done           → run completed; loop exits

const AGENTS_URL = "http://localhost:3737/agents/run";

async function runAgent(userQuery, callbacks = {}) {
  const {
    onStep         = () => {},
    onAssistantText = () => {},
    onFinalText    = () => {},
    onError        = () => {}
  } = callbacks;

  // Detect user TZ in the browser and forward to the server so meeting
  // times in the brief get rendered in the user's local zone.
  let userTimeZone = null;
  try {
    userTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    // Intl is universally available in Chrome; defensive only.
  }

  let res;
  try {
    res = await fetch(AGENTS_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "text/event-stream"
      },
      body: JSON.stringify({ query: userQuery, userTimeZone })
    });
  } catch (err) {
    const e = new Error(
      `Cannot reach agent service at ${AGENTS_URL}. ` +
      `Start it with: cd mcp-server && uv sync && uv run python server.py. ` +
      `(${err.message || err})`
    );
    onError(e);
    throw e;
  }

  if (!res.ok) {
    let detail = "";
    try { detail = await res.text(); } catch { /* ignore */ }
    const e = new Error(`Agent service HTTP ${res.status}: ${detail || res.statusText}`);
    onError(e);
    throw e;
  }

  // Stream SSE frames from the response body. Each frame is:
  //   event: <name>
  //   data: <single-line JSON>
  //   (blank line terminator)
  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let stopReason = null;
  let fatal = null;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      // Split on blank line — SSE frame delimiter.
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);

        const parsed = parseSseFrame(frame);
        if (!parsed) continue;

        switch (parsed.event) {
          case "step":
            onStep(parsed.data);
            break;
          case "assistant_text":
            onAssistantText(parsed.data.text || "", parsed.data.agent);
            break;
          case "final_text":
            if (parsed.data.text) onFinalText(parsed.data.text);
            break;
          case "error":
            fatal = new Error(parsed.data.message || "Agent run failed.");
            break;
          case "done":
            stopReason = parsed.data.stop_reason || "end_turn";
            break;
          default:
            // Unknown event names are ignored — forward compatibility.
            break;
        }
      }
    }
  } catch (err) {
    fatal = err instanceof Error ? err : new Error(String(err));
  }

  if (fatal) {
    onError(fatal);
    throw fatal;
  }

  return { stopReason: stopReason || "end_turn" };
}

// Parse a single SSE frame into {event, data}. Tolerates multi-line
// data: prefixes (joined with newline per the spec) even though the
// server emits single-line payloads.
function parseSseFrame(frame) {
  const lines = frame.split(/\r?\n/);
  let event = "message";
  const dataLines = [];
  for (const line of lines) {
    if (!line || line.startsWith(":")) continue; // empty or comment
    const colonIdx = line.indexOf(":");
    const field = colonIdx === -1 ? line : line.slice(0, colonIdx);
    const value = colonIdx === -1 ? "" : line.slice(colonIdx + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
    // "id" and "retry" fields are part of the spec but we don't use them.
  }
  if (!dataLines.length) return null;
  const raw = dataLines.join("\n");
  try {
    return { event, data: JSON.parse(raw) };
  } catch {
    return { event, data: { raw } };
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { runAgent, AGENTS_URL };
}
