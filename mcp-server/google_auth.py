"""Google OAuth 2.0 plumbing for the MCP server.

Flow:
  1. Tool handler needs Google credentials → calls `get_authorized_credentials(scopes)`.
  2. If a refresh token is on disk → load it, return ready-to-use Credentials.
  3. Otherwise → build a consent URL, auto-open the user's browser,
     raise so the agent surfaces a clear message.
  4. Browser hits /oauth/google/callback?code=... (registered in server.py)
     → handle_oauth_callback() exchanges the code, persists tokens.
  5. User retries their query in the extension; tokens are now on disk.

Tokens live in $HOME/.meeting-intel-mcp/google-tokens.json. The shape is
chosen to be readable and stable across both the legacy Node version and
this Python rewrite, so a user who already authorized once doesn't have
to re-authorize.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

TOKEN_DIR = Path.home() / ".meeting-intel-mcp"
TOKEN_FILE = TOKEN_DIR / "google-tokens.json"

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Every Google scope any tool in this server might need. We request the
# union of this set on every auth flow so the user sees ONE consent
# screen instead of being re-prompted the first time a Gmail tool runs
# after a Calendar-only auth. Per-tool scope hygiene is overkill for a
# single-user local dev tool, and the alternative is the agent looping
# on auth-required errors mid-query.
ALL_GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _default_redirect() -> str:
    port = os.environ.get("PORT", "3737")
    return f"http://localhost:{port}/oauth/google/callback"


# Module-level cache so successive tool calls share the same Credentials
# (and its in-memory access-token state).
_cached_creds: Credentials | None = None
_cached_scopes: list[str] | None = None
_lock = threading.Lock()

# google-auth-oauthlib's Flow auto-generates a PKCE code_verifier when
# authorization_url() is called, and Google's token endpoint requires
# the same verifier on the exchange. Because generate_auth_url() and
# handle_oauth_callback() build separate Flow instances, we have to
# stash the verifier between them — keyed by the OAuth `state` value,
# which Google echoes back on the redirect.
#
# We also stash the scope list we asked for: Flow.credentials populates
# `scopes` from oauth2session.scope, which is set at Flow init time. We
# init the callback Flow with scopes=None to accept whatever Google
# grants, but in some library versions that leaves creds.scopes empty —
# and an empty stored scope breaks the scope-coverage check on the next
# request, causing an infinite re-auth loop.
class _PendingAuth:
    __slots__ = ("verifier", "scopes")

    def __init__(self, verifier: str | None, scopes: list[str]) -> None:
        self.verifier = verifier
        self.scopes = scopes


_pending_pkce: dict[str, _PendingAuth] = {}


def _client_config() -> dict:
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in mcp-server/.env. "
            "See .env.example for setup instructions."
        )
    redirect = os.environ.get("GOOGLE_REDIRECT_URI") or _default_redirect()
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [redirect],
        }
    }


def generate_auth_url(scopes: list[str]) -> str:
    redirect = os.environ.get("GOOGLE_REDIRECT_URI") or _default_redirect()
    flow = Flow.from_client_config(_client_config(), scopes=scopes, redirect_uri=redirect)
    url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",  # always get a refresh_token, even on re-auth
        include_granted_scopes="true",
    )
    # Stash whatever the library generated (PKCE verifier) plus the scope
    # list we asked for, both keyed by state. The callback uses verifier
    # to satisfy Google's PKCE check, and falls back to `scopes` if the
    # token response doesn't surface them on creds.scopes.
    verifier = getattr(flow, "code_verifier", None)
    if state:
        _pending_pkce[state] = _PendingAuth(verifier=verifier, scopes=list(scopes))
    return url


async def handle_oauth_callback(code: str, state: str | None = None) -> dict:
    """Exchange an authorization code (from the OAuth redirect) for tokens
    and persist them. Called from the /oauth/google/callback route.

    `state` is the OAuth state Google echoes back; we use it to recover
    the PKCE code_verifier that was generated during generate_auth_url.
    """

    def _exchange() -> Credentials:
        # Build the flow without scopes so token_endpoint accepts the granted
        # scope set Google returns (which may differ if the user unticked one).
        redirect = os.environ.get("GOOGLE_REDIRECT_URI") or _default_redirect()
        flow = Flow.from_client_config(_client_config(), scopes=None, redirect_uri=redirect)
        pending: _PendingAuth | None = None
        if state:
            pending = _pending_pkce.pop(state, None)
            if pending and pending.verifier:
                flow.code_verifier = pending.verifier
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Library versions vary: some populate creds.scopes from
        # oauth2session.scope (which is empty when the flow was init'd
        # with scopes=None), some don't. If we end up with empty scopes
        # we save `"scope": ""` to disk, which then breaks the scope-
        # coverage check on every subsequent request and triggers an
        # infinite re-auth loop. Recover scopes from the token response
        # (Google echoes the granted scope back), and fall back to what
        # we originally requested if the response doesn't carry them.
        if not creds.scopes:
            token_resp = getattr(flow.oauth2session, "token", None) or {}
            granted_raw = token_resp.get("scope")
            if isinstance(granted_raw, str) and granted_raw.strip():
                scopes_list = granted_raw.split()
            elif isinstance(granted_raw, (list, tuple)):
                scopes_list = [str(s) for s in granted_raw]
            elif pending and pending.scopes:
                scopes_list = pending.scopes
            else:
                scopes_list = []

            if scopes_list:
                creds = Credentials(
                    token=creds.token,
                    refresh_token=creds.refresh_token,
                    token_uri=creds.token_uri,
                    client_id=creds.client_id,
                    client_secret=creds.client_secret,
                    scopes=scopes_list,
                    expiry=creds.expiry,
                )
        return creds

    creds = await asyncio.to_thread(_exchange)

    if not creds.refresh_token:
        # Most likely cause: the user has already granted consent and Google
        # returned only an access token. The `prompt: "consent"` flag in
        # generate_auth_url is meant to prevent this.
        raise RuntimeError(
            "Google did not return a refresh_token. Revoke the app's access at "
            "https://myaccount.google.com/permissions and re-authorize."
        )

    save_tokens(creds)

    # Invalidate cache so the next get_authorized_credentials() picks up fresh tokens.
    global _cached_creds, _cached_scopes
    with _lock:
        _cached_creds = None
        _cached_scopes = None

    return _serialize_credentials(creds)


async def get_authorized_credentials(scopes: list[str]) -> Credentials:
    """Load tokens, attach to a Credentials object, return it. If no tokens
    are on disk, auto-open the consent URL and raise with instructions."""
    global _cached_creds, _cached_scopes

    with _lock:
        if _cached_creds is not None and _cached_scopes is not None and _scopes_covered(
            _cached_scopes, scopes
        ):
            cached = _cached_creds

        else:
            cached = None

    if cached is not None:
        await _maybe_refresh(cached)
        return cached

    stored = await asyncio.to_thread(load_tokens)
    if not stored or not stored.get("refresh_token"):
        # Request ALL scopes up front, not just the one this tool needs,
        # so the user sees one consent screen for the lifetime of the
        # token instead of one per Google-backed tool.
        upfront = sorted(set(ALL_GOOGLE_SCOPES) | set(scopes))
        await _trigger_auth_flow(upfront)
        raise RuntimeError(_auth_required_message(scopes))

    stored_scopes = (stored.get("scope") or "").split()
    if not stored_scopes:
        # Empty scope field in an existing token file — caused by an
        # earlier callback that didn't populate creds.scopes correctly
        # (fixed above; this branch rescues tokens written before the
        # fix). Trust the refresh_token covers what's needed; if Google
        # disagrees it'll surface as a permission error on the actual
        # API call rather than an infinite re-auth loop.
        stored_scopes = list(scopes)
        # Heal the file in place so the next reader sees the right thing.
        try:
            stored["scope"] = " ".join(stored_scopes)
            TOKEN_FILE.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        except OSError as err:
            print(f"[google-auth] could not heal empty scope in token file: {err}", flush=True)
    elif not _scopes_covered(stored_scopes, scopes):
        # Same idea as the no-token case: union with ALL_GOOGLE_SCOPES so
        # the user never has to come back for the next missing scope.
        union = sorted(set(stored_scopes) | set(scopes) | set(ALL_GOOGLE_SCOPES))
        await _trigger_auth_flow(union)
        raise RuntimeError(_auth_required_message(scopes, scope_upgrade=True))

    creds = _credentials_from_dict(stored, scopes=stored_scopes)
    await _maybe_refresh(creds)

    with _lock:
        _cached_creds = creds
        _cached_scopes = stored_scopes
    return creds


async def _maybe_refresh(creds: Credentials) -> None:
    """If the access token is expired (or about to be), refresh it on a
    worker thread and persist the new tokens. google-auth refreshes
    synchronously, so we delegate to a thread to avoid blocking asyncio."""
    if creds.valid:
        return
    if not creds.refresh_token:
        return

    def _do_refresh() -> None:
        creds.refresh(Request())
        save_tokens(creds)

    await asyncio.to_thread(_do_refresh)


async def _trigger_auth_flow(scopes: list[str]) -> None:
    url = generate_auth_url(scopes)
    try:
        await asyncio.to_thread(webbrowser.open, url, 1, True)
    except Exception:
        # Browser unavailable (headless env). The thrown error message
        # includes the URL so the user can click manually.
        pass
    print(f"[google-auth] opened browser to consent URL: {url}", flush=True)


def _auth_required_message(scopes: list[str], *, scope_upgrade: bool = False) -> str:
    port = os.environ.get("PORT", "3737")
    url = f"http://localhost:{port}/auth/google?scope={'+'.join(scopes)}"
    if scope_upgrade:
        return (
            f"Google authorization needs additional scopes ({', '.join(scopes)}). "
            f"Opened a consent page in your browser. If it didn't open, visit {url}. "
            "Retry your query after authorizing."
        )
    return (
        "Google authorization required. Opened a consent page in your browser. "
        f"If it didn't open, visit {url}. Retry your query after authorizing."
    )


def _scopes_covered(have: list[str], need: list[str]) -> bool:
    have_set = set(have)
    return all(s in have_set for s in need)


# ---------- Token persistence ----------

def load_tokens() -> dict | None:
    try:
        raw = TOKEN_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return json.loads(raw)


def save_tokens(creds: Credentials) -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    payload = _serialize_credentials(creds)

    # Merge with anything already on disk so we don't lose fields the
    # legacy Node version may have written that the Python client doesn't
    # use directly.
    existing = load_tokens() or {}
    existing.update({k: v for k, v in payload.items() if v is not None})
    TOKEN_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    # chmod 600 — no-op on Windows, hardens on POSIX.
    try:
        TOKEN_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _serialize_credentials(creds: Credentials) -> dict:
    scope_str = " ".join(creds.scopes or [])
    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "scope": scope_str,
        "token_type": "Bearer",
        # Expiry as epoch-ms to match the Node version (which stored Date.getTime()).
        "expiry_date": int(creds.expiry.replace(tzinfo=timezone.utc).timestamp() * 1000)
        if creds.expiry
        else None,
    }


def _credentials_from_dict(stored: dict, *, scopes: list[str]) -> Credentials:
    expiry = None
    if stored.get("expiry_date"):
        expiry = datetime.fromtimestamp(stored["expiry_date"] / 1000.0, tz=timezone.utc).replace(
            tzinfo=None
        )

    return Credentials(
        token=stored.get("access_token"),
        refresh_token=stored.get("refresh_token"),
        token_uri=GOOGLE_TOKEN_URI,
        client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        scopes=scopes,
        expiry=expiry,
    )


def clear_tokens() -> None:
    """Exposed for diagnostics / future 'logout' tool."""
    global _cached_creds, _cached_scopes
    with _lock:
        _cached_creds = None
        _cached_scopes = None
    try:
        TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass
