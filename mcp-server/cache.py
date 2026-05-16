"""In-memory LRU keyed by string. Capped to MAX entries so a long-running
server doesn't grow unbounded. No persistence — restart = empty cache.

Used by tools to dedupe repeat MCP calls within a single popup session
(same attendee profiled twice, same company looked up across tools,
etc.) — the savings come from skipping SerpAPI / Gemini calls for
queries we just answered.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Awaitable, Callable, TypeVar

MAX = 50

T = TypeVar("T")

# Coroutine results are cached, not the coroutines themselves; an inflight
# map prevents two concurrent callers for the same key from both running
# `fn` (a thundering-herd cure for, e.g., parallel attendee lookups).
_store: OrderedDict[str, object] = OrderedDict()
_inflight: dict[str, asyncio.Future] = {}


async def with_cache(key: str, fn: Callable[[], Awaitable[T]]) -> T:
    """Wrap an async producer with cache. Cache hit returns the stored value
    directly; miss runs `fn`, caches the resolved value, and returns it.
    Errors bypass the cache so transient failures don't poison subsequent
    lookups."""
    if key in _store:
        # Bump to most-recently-used.
        value = _store.pop(key)
        _store[key] = value
        return value  # type: ignore[return-value]

    if key in _inflight:
        return await _inflight[key]  # type: ignore[return-value]

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _inflight[key] = fut
    try:
        value = await fn()
    except BaseException as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    else:
        if not fut.done():
            fut.set_result(value)
        _store[key] = value
        while len(_store) > MAX:
            # OrderedDict iterates in insertion order — popitem(last=False) is oldest.
            _store.popitem(last=False)
        return value
    finally:
        _inflight.pop(key, None)
