// In-memory LRU keyed by string. Capped to MAX entries so a long-running
// server doesn't grow unbounded. No persistence — restart = empty cache.
//
// Used by handlers to dedupe repeat tool calls within a single popup
// session (same attendee profiled twice, same company looked up across
// tools, etc.) — the savings come from skipping SerpAPI / Gemini calls
// for queries we just answered.

const MAX = 50;
const store = new Map();

// Wrap an async producer with cache. Cache hit returns the stored value
// directly; miss runs `fn`, caches the resolved value, and returns it.
// Errors bypass the cache so transient failures don't poison subsequent
// lookups.
export async function withCache(key, fn) {
  if (store.has(key)) {
    const v = store.get(key);
    // Bump to most-recently-used by re-inserting.
    store.delete(key);
    store.set(key, v);
    return v;
  }
  const value = await fn();
  store.set(key, value);
  while (store.size > MAX) {
    // Map iterates in insertion order — the first key is the oldest.
    const oldest = store.keys().next().value;
    store.delete(oldest);
  }
  return value;
}
