"""Gemini API wrapper for server-side reasoning calls.

Used by handlers that want LLM synthesis or "do you know this entity?"
gating before paying SerpAPI quota. Narrower contract than the
extension's api.js: structured JSON, low temperature, single turn,
no tool calls.

Either GEMINI_API_KEY or GOOGLE_API_KEY (the Google AI Studio default
name) is accepted. Read at call time so dotenv has had a chance to
populate the env at startup.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


async def gemini_ask_json(
    prompt: str, *, temperature: float = 0.0, max_tokens: int = 1024
) -> Any:
    """Ask Gemini for a JSON-shaped response. The prompt should describe the
    expected schema explicitly. Returns the parsed object; raises on
    invalid JSON or API failure."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set in mcp-server/.env "
            "(see .env.example). The server has no fallback for this path."
        )
    if not prompt or not isinstance(prompt, str):
        raise ValueError("gemini_ask_json requires a non-empty prompt string.")

    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        res = await client.post(
            GEMINI_API_URL,
            content=json.dumps(body),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": key,
            },
        )

    if res.status_code != 200:
        snippet = _truncate(res.text or res.reason_phrase, 250)
        raise RuntimeError(f"Gemini {res.status_code}: {snippet}")

    data = res.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise RuntimeError(f"Gemini returned no usable response ({reason}).")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text") or "" for p in parts)
    if not text:
        finish = candidate.get("finishReason") or "unknown"
        raise RuntimeError(f"Gemini returned empty content (finishReason: {finish}).")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise RuntimeError(f"Gemini did not return valid JSON: {_truncate(text, 200)}")


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"
