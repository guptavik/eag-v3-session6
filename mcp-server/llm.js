// Gemini API wrapper for server-side reasoning calls.
//
// Used by handlers that want LLM synthesis or "do you know this entity?"
// gating before paying SerpAPI quota. Lives separately from the
// extension's api.js because the contract is much narrower: structured
// JSON requests, low temperature, single-turn, no tool calls.
//
// Either GEMINI_API_KEY or GOOGLE_API_KEY (the Google AI Studio default
// name) is accepted. Read at call time so dotenv has had a chance to
// populate the env via index.js.

const GEMINI_MODEL   = "gemini-2.5-flash";
const GEMINI_API_URL = `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent`;

// Ask Gemini for a JSON-shaped response. The prompt should describe the
// expected schema explicitly. Returns the parsed object; throws if the
// response isn't valid JSON or the API call fails.
export async function geminiAskJson(prompt, opts = {}) {
  const key = process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY;
  if (!key) {
    throw new Error(
      "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set in mcp-server/.env " +
      "(see .env.example). The server has no fallback for this path."
    );
  }
  if (!prompt || typeof prompt !== "string") {
    throw new Error("geminiAskJson requires a non-empty prompt string.");
  }

  const body = {
    contents: [{ role: "user", parts: [{ text: prompt }] }],
    generationConfig: {
      temperature: opts.temperature ?? 0,        // deterministic for structured tasks
      maxOutputTokens: opts.maxTokens ?? 1024,
      responseMimeType: "application/json"
    }
  };

  const res = await fetch(GEMINI_API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-goog-api-key": key
    },
    body: JSON.stringify(body)
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Gemini ${res.status}: ${truncate(text || res.statusText, 250)}`);
  }

  const data = await res.json();
  const candidate = data.candidates?.[0];
  if (!candidate) {
    const reason = data.promptFeedback?.blockReason || "no candidates";
    throw new Error(`Gemini returned no usable response (${reason}).`);
  }

  const text = candidate.content?.parts?.map(p => p.text).filter(Boolean).join("") || "";
  if (!text) {
    throw new Error(`Gemini returned empty content (finishReason: ${candidate.finishReason || "unknown"}).`);
  }

  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`Gemini did not return valid JSON: ${truncate(text, 200)}`);
  }
}

function truncate(s, n) {
  return s.length > n ? s.slice(0, n) + "…" : s;
}
