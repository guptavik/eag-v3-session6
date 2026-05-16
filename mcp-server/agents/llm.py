"""Multi-turn Gemini wrapper for the server-side agent loop.

Distinct from the existing top-level llm.py which only does single-shot
JSON-mode calls. This one:
  - Speaks Gemini's native message shape (contents / parts /
    functionCall / functionResponse) end-to-end.
  - Accepts tool declarations and returns the model's tool_use blocks.
  - Sanitizes JSON-Schema tool parameters to Gemini's OpenAPI subset
    ($schema, additionalProperties stripped; ["string","null"] →
    "string" + nullable=true), same translation the old api.js did.

Returns a small response object the SubAgent loop consumes:
    {
        "content":      [ TextPart | ToolUseBlock ],
        "stop_reason":  "tool_use" | "end_turn"
    }

TextPart    = {"type": "text",     "text": str}
ToolUseBlock= {"type": "tool_use", "id": str, "name": str, "input": dict}
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

MAX_OUTPUT_TOKENS = 8192
_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


async def call_llm(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    system_prompt: str,
    user_time_zone: str | None = None,
) -> dict[str, Any]:
    """Send one turn to Gemini with the agent's tool list.

    `messages` is the in-memory conversation history in Gemini's native
    shape (each entry is {"role": "user"|"model", "parts": [...]}).
    SubAgent owns the history list and mutates it across turns.

    `tools` is a list of tool descriptors with `name`, `description`,
    `input_schema`. We translate them to Gemini's functionDeclarations
    shape on the fly.
    """
    key = _resolve_api_key()

    system_text = system_prompt
    if user_time_zone:
        system_text = (
            f"{system_prompt}\n\n"
            "## User context\n"
            f"The user's local timezone is **{user_time_zone}**. Tool results contain "
            "ISO timestamps with their original UTC offset (e.g. "
            "\"2026-05-03T14:00:00-05:00\") and may include a per-meeting `timeZone` "
            "field. When you write the final brief, convert all meeting times to the "
            "user's local timezone and include the timezone abbreviation, e.g. "
            "\"Mon, May 5, 2:00 PM CST\". Do the conversion in your response — do not "
            "ask a tool to do it."
        )

    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": messages,
        "tools": [{"functionDeclarations": [_tool_to_function_decl(t) for t in tools]}],
        "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
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
        snippet = _truncate(res.text or res.reason_phrase, 500)
        raise RuntimeError(f"Gemini API error {res.status_code}: {snippet}")

    return _normalize_response(res.json())


# ---------------------------------------------------------------------------
# Message construction helpers — SubAgent.run() uses these to build the
# Gemini-native message list as it accumulates turns and tool results.
# ---------------------------------------------------------------------------


def make_user_text_message(text: str) -> dict[str, Any]:
    return {"role": "user", "parts": [{"text": text}]}


def make_tool_results_message(results: list[dict[str, Any]]) -> dict[str, Any]:
    """`results` items are dicts with `name` + `response` (a JSON-able
    object) and optional `is_error`. Emitted as functionResponse parts
    in a single user turn (Gemini's convention)."""
    parts: list[dict[str, Any]] = []
    for r in results:
        payload = r.get("response")
        if r.get("is_error"):
            payload = {"error": r.get("error_message") or "tool error"}
        if not isinstance(payload, dict):
            # Gemini wants the response value to be a JSON object.
            payload = {"result": payload}
        parts.append({"functionResponse": {"name": r["name"], "response": payload}})
    return {"role": "user", "parts": parts}


# ---------------------------------------------------------------------------
# Internal: tool schema translation + response normalization.
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set in mcp-server/.env. "
            "Add it and restart the server — the agent loop needs it."
        )
    return key


def _tool_to_function_decl(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert our internal tool descriptor to Gemini's functionDeclaration."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "parameters": _sanitize_schema_for_gemini(tool.get("input_schema") or {}),
    }


def _sanitize_schema_for_gemini(schema: Any) -> Any:
    """Gemini's parameters schema is an OpenAPI 3.0 subset, not full JSON
    Schema. Strip $schema / additionalProperties (Gemini 400s on them)
    and convert ["string","null"] nullable shorthand to "string" +
    nullable: true. Same translation the old api.js sanitizeSchemaForGemini
    did, kept verbatim here so behavior is identical."""
    if isinstance(schema, list):
        return [_sanitize_schema_for_gemini(item) for item in schema]
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for k, v in schema.items():
            if k in ("$schema", "additionalProperties"):
                continue
            if k == "type" and isinstance(v, list):
                non_null = [t for t in v if t != "null"]
                has_null = "null" in v
                out["type"] = non_null[0] if len(non_null) == 1 else non_null
                if has_null:
                    out["nullable"] = True
                continue
            out[k] = _sanitize_schema_for_gemini(v)
        return out
    return schema


def _normalize_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert Gemini's response into the {content, stop_reason} shape
    the SubAgent loop expects. We pick the first candidate."""
    candidates = payload.get("candidates") or []
    if not candidates:
        reason = (payload.get("promptFeedback") or {}).get("blockReason", "unknown")
        raise RuntimeError(f"Gemini returned no candidates (blockReason: {reason})")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    content: list[dict[str, Any]] = []
    has_tool_use = False

    for p in parts:
        if isinstance(p.get("text"), str) and p["text"]:
            content.append({"type": "text", "text": p["text"]})
        elif "functionCall" in p:
            has_tool_use = True
            fc = p["functionCall"]
            content.append({
                "type": "tool_use",
                "id": f"gem_{uuid.uuid4().hex[:8]}",
                "name": fc.get("name", "unknown"),
                "input": fc.get("args") or {},
            })

    if not content:
        finish = candidate.get("finishReason") or "unknown"
        raise RuntimeError(f"Gemini response had no usable content (finishReason: {finish})")

    return {
        "content": content,
        "stop_reason": "tool_use" if has_tool_use else "end_turn",
    }


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"
