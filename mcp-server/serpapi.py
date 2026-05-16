"""SerpAPI client (Google SERP scraper).

We hit the standard /search endpoint with the `google` engine and flatten
organic_results into the same {title, description, url} shape the rest
of the server expects.

Free tier: 100 searches/month, 250/hour. No per-second throttle needed.

`SERPAPI_API_KEY` is read at call time so dotenv has had a chance to
populate it via server startup.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel, Field, HttpUrl

SERPAPI_URL = "https://serpapi.com/search"
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class SerpResult(BaseModel):
    """One organic result flattened to the shape used elsewhere in the server."""

    title: str = ""
    description: str = ""
    url: str = ""


async def serp_search(query: str, *, count: int = 5) -> list[SerpResult]:
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        raise RuntimeError(
            "SERPAPI_API_KEY is not set. Add it to mcp-server/.env (see .env.example) "
            "and restart the server."
        )
    if not query or not isinstance(query, str):
        raise ValueError("serp_search requires a non-empty query string.")

    params = {
        "q": query,
        "engine": "google",
        "api_key": key,
        "num": str(count),
        "safe": "active",
    }

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        res = await client.get(SERPAPI_URL, params=params, headers={"Accept": "application/json"})

    if res.status_code != 200:
        snippet = _truncate(res.text or res.reason_phrase, 200)
        raise RuntimeError(f"SerpAPI {res.status_code}: {snippet}")

    data: dict[str, Any] = res.json()
    # SerpAPI returns 200 even on quota / config errors — surface them.
    if "error" in data:
        raise RuntimeError(f"SerpAPI error: {data['error']}")

    items = data.get("organic_results") or []
    return [
        SerpResult(
            title=r.get("title") or "",
            description=r.get("snippet") or "",
            url=r.get("link") or "",
        )
        for r in items
    ]


def find_linkedin_result(results: list[SerpResult]) -> SerpResult | None:
    """Detect a LinkedIn URL among results. Profile and company URLs both
    live under linkedin.com — caller decides which is appropriate."""
    for r in results:
        if "linkedin.com" in r.url.lower():
            return r
    return None


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…"
