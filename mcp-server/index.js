// HTTP entry point for the MCP server.
//
// Exposes the meeting-intelligence MCP server on POST /mcp via the
// SDK's StreamableHTTPServerTransport. Stateless mode: a fresh
// server + transport pair per request, fully isolated. Good fit
// for a single-user dev tool talking to a Chrome extension popup
// whose lifetime is shorter than any meaningful session.

// Load .env first so handlers can read process.env at call time.
import "dotenv/config";

import express from "express";
import cors from "cors";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createServer } from "./server.js";
import { generateAuthUrl, handleOAuthCallback } from "./google-auth.js";

// Default scopes the /auth/google endpoint requests when no `scope` query
// param is supplied. Tools individually request just what they need; this
// default only matters when the user visits /auth/google directly to
// pre-authorize everything in one trip.
const DEFAULT_GOOGLE_SCOPES = [
  "https://www.googleapis.com/auth/calendar.readonly",
  "https://www.googleapis.com/auth/gmail.readonly"
];

const PORT = Number(process.env.PORT) || 3737;

const app = express();

// Allow the Chrome extension popup (origin: chrome-extension://<id>) and
// any other local dev client. The extension's host_permissions cover
// auth at the manifest level.
app.use(cors({
  origin: true,
  exposedHeaders: ["Mcp-Session-Id"],
  allowedHeaders: ["Content-Type", "Accept", "Mcp-Session-Id"]
}));
app.use(express.json({ limit: "4mb" }));

app.get("/health", (_req, res) => {
  res.json({ ok: true, name: "meeting-intelligence-mcp", port: PORT });
});

// ---------- Google OAuth ----------
//
// /auth/google              → 302 → Google consent screen
// /oauth/google/callback    ← Google redirects here with ?code=...

app.get("/auth/google", (req, res) => {
  const scopes = req.query.scope
    ? String(req.query.scope).split(/\s+/).filter(Boolean)
    : DEFAULT_GOOGLE_SCOPES;
  try {
    const url = generateAuthUrl(scopes);
    res.redirect(url);
  } catch (err) {
    res.status(500).send(htmlPage("Configuration error", err.message));
  }
});

app.get("/oauth/google/callback", async (req, res) => {
  const { code, error } = req.query;
  if (error) {
    return res.status(400).send(htmlPage("Authorization failed", String(error)));
  }
  if (!code) {
    return res.status(400).send(htmlPage("Authorization failed", "Missing authorization code."));
  }
  try {
    await handleOAuthCallback(String(code));
    res.send(htmlPage(
      "Google authorization complete",
      "You can close this tab and retry your query in the extension."
    ));
  } catch (err) {
    console.error("[oauth] callback error:", err);
    res.status(500).send(htmlPage("Authorization failed", err.message));
  }
});

function htmlPage(heading, body) {
  return `<!doctype html><html><body style="font-family:system-ui,sans-serif;padding:40px;max-width:560px;margin:auto;color:#202124"><h2>${heading}</h2><p>${body}</p></body></html>`;
}

// MCP endpoint. POST is the protocol's primary method; GET/DELETE are
// part of the streamable-http spec for stateful sessions but we run
// stateless, so the transport will respond appropriately.
app.all("/mcp", async (req, res) => {
  let transport;
  let server;
  try {
    server = createServer();
    transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined  // stateless: every request stands alone
    });

    res.on("close", () => {
      try { transport?.close?.(); } catch {}
      try { server?.close?.(); } catch {}
    });

    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch (err) {
    console.error("[mcp] request handler error:", err);
    if (!res.headersSent) {
      res.status(500).json({
        jsonrpc: "2.0",
        error: { code: -32603, message: `Internal server error: ${err.message}` },
        id: req.body?.id ?? null
      });
    }
    try { transport?.close?.(); } catch {}
    try { server?.close?.(); } catch {}
  }
});

app.listen(PORT, () => {
  console.log(`[mcp] meeting-intelligence MCP server listening`);
  console.log(`[mcp]   endpoint:  http://localhost:${PORT}/mcp`);
  console.log(`[mcp]   health:    http://localhost:${PORT}/health`);
});
