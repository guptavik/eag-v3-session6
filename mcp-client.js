// Minimal MCP-over-HTTP client for the extension popup.
//
// Speaks JSON-RPC 2.0 against the streamable-http endpoint exposed by
// mcp-server/. The server replies in SSE framing (event:/data: lines)
// even for one-shot calls, so we parse both shapes.
//
// Stateless: every call is an independent POST. Server runs without
// session IDs. We send `initialize` once per popup load to negotiate
// protocol version and cache the result; if the server restarts mid-
// session and rejects a later call, the next reload re-handshakes.

const MCP_URL = "http://localhost:3737/mcp";
const MCP_PROTOCOL_VERSION = "2025-03-26";
const MCP_CLIENT_INFO = { name: "meeting-intel-extension", version: "0.2.0" };

let initialized = false;
let nextRpcId = 1;

async function mcpRequest(method, params) {
  const id = nextRpcId++;
  const body = { jsonrpc: "2.0", id, method, params: params || {} };

  let res;
  try {
    res = await fetch(MCP_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
      },
      body: JSON.stringify(body)
    });
  } catch (err) {
    throw new Error(
      `Cannot reach MCP server at ${MCP_URL}. ` +
      `Start it with: cd mcp-server && npm install && npm start. ` +
      `(${err.message || err})`
    );
  }

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`MCP HTTP ${res.status}: ${text || res.statusText}`);
  }

  const ct = res.headers.get("content-type") || "";
  const payload = ct.includes("text/event-stream")
    ? await readJsonRpcFromSse(res, id)
    : await res.json();

  if (payload.error) {
    const e = payload.error;
    const data = e.data ? ` (${typeof e.data === "string" ? e.data : JSON.stringify(e.data)})` : "";
    throw new Error(`MCP error ${e.code}: ${e.message}${data}`);
  }
  return payload.result;
}

// Read the SSE-framed body and return the first JSON-RPC message whose
// id matches what we sent. Notifications (no id) are skipped.
async function readJsonRpcFromSse(res, expectedId) {
  const text = await res.text();
  const lines = text.split(/\r?\n/);
  for (const line of lines) {
    if (!line.startsWith("data:")) continue;
    const json = line.slice(5).trim();
    if (!json) continue;
    let parsed;
    try { parsed = JSON.parse(json); } catch { continue; }
    if (parsed && parsed.id === expectedId) return parsed;
  }
  throw new Error("MCP SSE response did not contain a matching JSON-RPC reply");
}

async function ensureInitialized() {
  if (initialized) return;
  await mcpRequest("initialize", {
    protocolVersion: MCP_PROTOCOL_VERSION,
    capabilities: {},
    clientInfo: MCP_CLIENT_INFO
  });
  // The protocol expects a `notifications/initialized` notification next.
  // In stateless mode the server doesn't track session state, so the
  // notification is harmless to skip; we mark client-side only.
  initialized = true;
}

// Convert the MCP tool list into the Anthropic-style schema the agent
// loop has used since the start (TOOLS array shape in tools.js).
async function mcpListTools() {
  await ensureInitialized();
  const result = await mcpRequest("tools/list", {});
  return (result.tools || []).map(t => ({
    name: t.name,
    description: t.description,
    input_schema: t.inputSchema
  }));
}

// Call a tool and return its JSON payload (parsed). MCP tools return an
// array of content blocks; our server always emits a single text block
// containing the JSON-stringified result, so we unwrap that here.
//
// We auto-inject the browser-detected user timezone into every tool
// call's arguments. Tools that care (calculateMeetingStats) read it to
// compute day-of-week in the user's local zone instead of the server's;
// tools that don't care have Zod silently strip the extra field. This
// keeps the agent from having to remember to forward the TZ each call.
async function mcpCallTool(name, args) {
  await ensureInitialized();

  const finalArgs = { ...(args || {}) };
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (tz) finalArgs.userTimeZone = tz;  // overrides anything the agent guessed
  } catch {
    // Intl is universally available in Chrome; defensive fallback only.
  }

  const result = await mcpRequest("tools/call", {
    name,
    arguments: finalArgs
  });

  if (result.isError) {
    const text = result.content?.[0]?.text || "tool returned an error";
    throw new Error(text);
  }

  const block = result.content?.[0];
  if (block && block.type === "text" && typeof block.text === "string") {
    try { return JSON.parse(block.text); }
    catch { return block.text; }
  }
  // Fallback: return the raw content array if the server ever sends a
  // shape we don't recognize, so the agent can still see something.
  return result.content;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { mcpListTools, mcpCallTool };
}
