// Google OAuth 2.0 plumbing for the MCP server.
//
// Flow:
//   1. Tool handler needs Google credentials → calls getAuthorizedClient(scopes).
//   2. If a refresh token is on disk → load it, return a ready-to-use client.
//   3. Otherwise → build a consent URL, auto-open the user's browser,
//      throw an error so the agent surfaces a clear message to the user.
//   4. Browser hits /oauth/google/callback?code=... (registered in index.js)
//      → handleOAuthCallback() exchanges the code, persists tokens.
//   5. User retries their query in the extension; tokens are now on disk.
//
// Tokens live in $HOME/.meeting-intel-mcp/google-tokens.json. Auto-refreshed
// access tokens are persisted via the OAuth2 client's "tokens" event so we
// don't lose them between requests.

import { google } from "googleapis";
import { promises as fs } from "fs";
import path from "path";
import os from "os";

const TOKEN_DIR  = path.join(os.homedir(), ".meeting-intel-mcp");
const TOKEN_FILE = path.join(TOKEN_DIR, "google-tokens.json");

const DEFAULT_REDIRECT = `http://localhost:${process.env.PORT || 3737}/oauth/google/callback`;

// Module-level cache so successive tool calls in the same process share
// the same OAuth2 client (and its in-memory token state).
let cachedClient = null;
let cachedScopes = null;

function buildOAuthClient() {
  const clientId     = process.env.GOOGLE_CLIENT_ID;
  const clientSecret = process.env.GOOGLE_CLIENT_SECRET;
  const redirectUri  = process.env.GOOGLE_REDIRECT_URI || DEFAULT_REDIRECT;

  if (!clientId || !clientSecret) {
    throw new Error(
      "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in mcp-server/.env. " +
      "See .env.example for setup instructions."
    );
  }

  const client = new google.auth.OAuth2(clientId, clientSecret, redirectUri);

  // googleapis auto-refreshes the access token when it's expired, but it
  // does NOT auto-persist the new credentials. Subscribe so the refreshed
  // access_token + expiry_date land back on disk.
  client.on("tokens", async (newTokens) => {
    try {
      const existing = await loadTokens().catch(() => ({}));
      await saveTokens({ ...(existing || {}), ...newTokens });
    } catch (err) {
      console.error("[google-auth] failed to persist refreshed tokens:", err.message);
    }
  });

  return client;
}

export function generateAuthUrl(scopes) {
  const client = buildOAuthClient();
  return client.generateAuthUrl({
    access_type: "offline",     // gives us a refresh_token
    prompt: "consent",          // force the consent screen so we always
                                // get a refresh_token, even on re-auth
    scope: scopes,
    include_granted_scopes: true
  });
}

// Exchange an authorization code (from the OAuth redirect) for tokens
// and persist them. Called from index.js's /oauth/google/callback route.
export async function handleOAuthCallback(code) {
  const client = buildOAuthClient();
  const { tokens } = await client.getToken(code);
  if (!tokens.refresh_token) {
    // Most likely cause: the user has already granted consent and Google
    // returned only an access token. The `prompt: "consent"` flag in
    // generateAuthUrl is meant to prevent this.
    throw new Error(
      "Google did not return a refresh_token. Revoke the app's access at " +
      "https://myaccount.google.com/permissions and re-authorize."
    );
  }
  await saveTokens(tokens);
  // Invalidate cached client so the next getAuthorizedClient() call picks
  // up the fresh tokens.
  cachedClient = null;
  cachedScopes = null;
  return tokens;
}

// Load tokens, attach to a fresh OAuth2 client, return it. If no tokens
// are on disk, auto-open the consent URL and throw with instructions.
export async function getAuthorizedClient(scopes) {
  // Reuse the cached client if it covers the requested scopes.
  if (cachedClient && cachedScopes && scopesCovered(cachedScopes, scopes)) {
    return cachedClient;
  }

  const tokens = await loadTokens().catch(() => null);
  if (!tokens || !tokens.refresh_token) {
    await triggerAuthFlow(scopes);
    throw new Error(authRequiredMessage(scopes));
  }

  // If the stored tokens don't cover the requested scopes, force re-auth.
  const stored = (tokens.scope || "").split(/\s+/).filter(Boolean);
  if (!scopesCovered(stored, scopes)) {
    await triggerAuthFlow([...new Set([...stored, ...scopes])]);
    throw new Error(authRequiredMessage(scopes, true));
  }

  const client = buildOAuthClient();
  client.setCredentials(tokens);
  cachedClient = client;
  cachedScopes = stored;
  return client;
}

async function triggerAuthFlow(scopes) {
  const url = generateAuthUrl(scopes);
  // Dynamic import keeps the cold-start path light.
  try {
    const { default: open } = await import("open");
    await open(url);
  } catch {
    // open() failed (no browser available, headless env, etc.) — the
    // thrown error message includes the URL so the user can click manually.
  }
  console.log(`[google-auth] opened browser to consent URL: ${url}`);
}

function authRequiredMessage(scopes, scopeUpgrade = false) {
  const url = `http://localhost:${process.env.PORT || 3737}/auth/google?scope=${encodeURIComponent(scopes.join(" "))}`;
  return scopeUpgrade
    ? `Google authorization needs additional scopes (${scopes.join(", ")}). Opened a consent page in your browser. If it didn't open, visit ${url}. Retry your query after authorizing.`
    : `Google authorization required. Opened a consent page in your browser. If it didn't open, visit ${url}. Retry your query after authorizing.`;
}

function scopesCovered(haveScopes, needScopes) {
  const have = new Set(haveScopes);
  return needScopes.every(s => have.has(s));
}

// ---------- Token persistence ----------

async function loadTokens() {
  try {
    const raw = await fs.readFile(TOKEN_FILE, "utf8");
    return JSON.parse(raw);
  } catch (err) {
    if (err.code === "ENOENT") return null;
    throw err;
  }
}

async function saveTokens(tokens) {
  await fs.mkdir(TOKEN_DIR, { recursive: true });
  await fs.writeFile(TOKEN_FILE, JSON.stringify(tokens, null, 2), "utf8");
  // chmod 600 is a no-op on Windows; harmless to attempt on POSIX.
  try { await fs.chmod(TOKEN_FILE, 0o600); } catch {}
}

// Exposed for diagnostics / future "logout" tool.
export async function clearTokens() {
  cachedClient = null;
  cachedScopes = null;
  try { await fs.unlink(TOKEN_FILE); } catch {}
}
