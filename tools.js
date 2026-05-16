// Extension-side tool surface. The actual tool implementations live in
// the MCP server at http://localhost:3737/mcp (see mcp-server/).
//
// TOOLS is populated at popup startup by fetchToolDefinitions(), which
// pulls the live schema list from the server. agent.js reads the array
// directly when calling the LLM, so we mutate it in place rather than
// reassigning, keeping its reference stable.
//
// executeTool() forwards each invocation to the MCP server. Errors from
// the server are thrown so agent.js's retry/error path can handle them
// the same way it always has.

const TOOLS = [];

async function fetchToolDefinitions() {
  const fresh = await mcpListTools();
  TOOLS.length = 0;
  TOOLS.push(...fresh);
  return TOOLS;
}

async function executeTool(toolName, toolInput) {
  return mcpCallTool(toolName, toolInput || {});
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { TOOLS, fetchToolDefinitions, executeTool };
}
