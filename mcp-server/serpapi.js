// SerpAPI client (Google SERP scraper).
//
// We hit the standard /search endpoint with the `google` engine and
// flatten organic_results into the same {title, description, url}
// shape the rest of the server already expects — this file is a
// drop-in replacement for the M2 Brave client.
//
// Free tier: 100 searches/month, 250/hour. No per-second throttle
// required (unlike Brave's 1 req/sec free tier), so we omit one.
//
// process.env.SERPAPI_API_KEY is read at call time so dotenv has had
// a chance to populate it via index.js.

const SERPAPI_URL = "https://serpapi.com/search";

export async function serpSearch(query, opts = {}) {
  const key = process.env.SERPAPI_API_KEY;
  if (!key) {
    throw new Error(
      "SERPAPI_API_KEY is not set. Add it to mcp-server/.env (see .env.example) " +
      "and restart the server."
    );
  }
  if (!query || typeof query !== "string") {
    throw new Error("serpSearch requires a non-empty query string.");
  }

  const params = new URLSearchParams({
    q: query,
    engine: "google",
    api_key: key,
    num: String(opts.count ?? 5),
    safe: "active"
  });

  const res = await fetch(`${SERPAPI_URL}?${params.toString()}`, {
    method: "GET",
    headers: { "Accept": "application/json" }
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`SerpAPI ${res.status}: ${truncate(text || res.statusText, 200)}`);
  }

  const data = await res.json();
  // SerpAPI returns 200 even on quota / config errors — surface them.
  if (data.error) {
    throw new Error(`SerpAPI error: ${data.error}`);
  }

  const items = data.organic_results || [];
  return items.map(r => ({
    title: r.title || "",
    description: r.snippet || "",
    url: r.link || ""
  }));
}

// Detect a LinkedIn URL among results. Profile and company URLs both
// live under linkedin.com — caller decides which is appropriate.
export function findLinkedInResult(results) {
  return results.find(r => /\blinkedin\.com\b/i.test(r.url)) || null;
}

function truncate(s, n) {
  return s.length > n ? s.slice(0, n) + "…" : s;
}
