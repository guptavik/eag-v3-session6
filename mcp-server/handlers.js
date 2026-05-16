// Tool implementations backing the MCP server.
//
// Backends per tool:
//   getUpcomingMeetings        → Google Calendar
//   searchGmail                → Gmail API
//   searchWebInfo              → Gemini first, SerpAPI fallback
//                                (SerpAPI directly when query has freshness intent)
//   analyzeAttendeeBackground  → 0 calls for internal attendees
//                                else SerpAPI (LinkedIn URL) + Gemini (synthesis)
//   calculateMeetingStats      → pure computation
//
// All external lookups are wrapped in a process-local LRU (cache.js) so
// repeat queries within a popup session pay nothing.

import { google } from "googleapis";
import { serpSearch, findLinkedInResult } from "./serpapi.js";
import { getAuthorizedClient } from "./google-auth.js";
import { geminiAskJson } from "./llm.js";
import { withCache } from "./cache.js";

const CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"];
const GMAIL_SCOPES    = ["https://www.googleapis.com/auth/gmail.readonly"];

// Default lookahead per timeframe label, used by calculateMeetingStats
// when the agent passes only `timeframe` (no explicit hoursAhead, no
// meetings array).
const TIMEFRAME_HOURS = { today: 24, week: 168, month: 720 };

// Phrases that indicate the user wants fresh, post-cutoff information.
// When the query matches, we skip Gemini (knowledge-cutoff Jan 2026)
// and go straight to SerpAPI.
const FRESHNESS_PATTERN = /\b(news|recent|latest|today|yesterday|this\s+(week|month|quarter|year)|current|now|funding|announce(d|ment)?|just\s+launched|2026|2027|2028)\b/i;

// Comma-separated list of email domains treated as "internal" — attendees
// from these domains get a stub profile with zero API calls.
function getOwnDomains() {
  return (process.env.OWN_COMPANY_DOMAIN || "")
    .split(",")
    .map(d => d.trim().toLowerCase())
    .filter(Boolean);
}

function isOwnAttendee(email) {
  const domains = getOwnDomains();
  if (!domains.length) return false;
  const emailDomain = (email.split("@")[1] || "").toLowerCase();
  return domains.includes(emailDomain);
}

// ---------- Tool handlers ----------

// Returns a Date representing the end of the current calendar day in the
// given IANA timezone. Works by computing how many seconds remain until
// local midnight and adding that to the current UTC time.
function endOfDayInTZ(now, tz) {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    hour: "numeric", minute: "2-digit", second: "2-digit",
    hour12: false
  });
  const parts = Object.fromEntries(fmt.formatToParts(now).map(p => [p.type, p.value]));
  const h = +parts.hour || 0;
  const m = +parts.minute || 0;
  const s = +parts.second || 0;
  const secsToMidnight = 24 * 3600 - (h * 3600 + m * 60 + s);
  return new Date(now.getTime() + secsToMidnight * 1000);
}

export async function getUpcomingMeetings({ hoursAhead = 24, endOfToday = false, userTimeZone } = {}) {
  const auth = await getAuthorizedClient(CALENDAR_SCOPES);
  const calendar = google.calendar({ version: "v3", auth });

  const now    = new Date();
  const cutoff = endOfToday && userTimeZone
    ? endOfDayInTZ(now, userTimeZone)
    : new Date(now.getTime() + hoursAhead * 3600 * 1000);

  const res = await calendar.events.list({
    calendarId: "primary",
    timeMin: now.toISOString(),
    timeMax: cutoff.toISOString(),
    singleEvents: true,        // expand recurring events into individual instances
    orderBy: "startTime",
    maxResults: 50
  });

  return (res.data.items || [])
    .filter(e => e.status !== "cancelled")
    .map(e => ({
      id: e.id,
      title: e.summary || "(no title)",
      // Preserve the original ISO with offset (e.g. "2026-05-03T14:00:00-05:00")
      // so downstream consumers can convert to the user's local TZ
      // without losing information.
      startTime: normalizeEventTime(e.start),
      endTime:   normalizeEventTime(e.end),
      // IANA name from the event itself, when set (Calendar populates
      // this for timed events). Useful when the LLM wants to label the
      // brief with the meeting's "home" zone.
      timeZone:  e.start?.timeZone || e.end?.timeZone || null,
      attendees: (e.attendees || [])
        .filter(a => a.email && !a.self && !a.resource)
        .map(a => a.email),
      location: e.location || null,
      description: e.description || ""
    }))
    .filter(m => m.startTime);  // drop events with unparseable times
}

// Calendar API returns either start.dateTime (ISO with offset) for timed
// events or start.date (YYYY-MM-DD) for all-day events. Keep timed
// events as-is so the offset is preserved; synthesize UTC midnight for
// all-day events so callers can still parse them as Dates.
function normalizeEventTime(t) {
  if (!t) return null;
  if (t.dateTime) return t.dateTime;
  if (t.date)     return `${t.date}T00:00:00Z`;
  return null;
}

export async function searchGmail({ query, maxResults = 5 }) {
  if (!query || typeof query !== "string") {
    throw new Error("searchGmail requires a non-empty string 'query'.");
  }

  const auth = await getAuthorizedClient(GMAIL_SCOPES);
  const gmail = google.gmail({ version: "v1", auth });

  // messages.list accepts Gmail's native query syntax (same as the
  // search box in the Gmail UI). The agent passes free text, which
  // Gmail treats as an AND of terms — good enough for our use.
  const list = await gmail.users.messages.list({
    userId: "me",
    q: query,
    maxResults: Math.max(1, Math.min(maxResults, 20))
  });

  const ids = (list.data.messages || []).map(m => m.id);
  if (ids.length === 0) return [];

  // Pull just the headers we need (and the snippet, which is included
  // automatically by `format: "metadata"`). Parallel fetches — Gmail
  // is fine with concurrent reads from a single user.
  const messages = await Promise.all(ids.map(id =>
    gmail.users.messages.get({
      userId: "me",
      id,
      format: "metadata",
      metadataHeaders: ["Subject", "From", "Date"]
    }).then(r => r.data)
  ));

  return messages.map(m => {
    const headers = m.payload?.headers || [];
    const get = (name) =>
      headers.find(h => h.name?.toLowerCase() === name.toLowerCase())?.value || "";

    const dateRaw = get("Date");
    let date = "";
    if (dateRaw) {
      const d = new Date(dateRaw);
      if (!isNaN(d)) date = d.toISOString().slice(0, 10);
    }

    return {
      subject: get("Subject") || "(no subject)",
      from: get("From"),
      date,
      snippet: m.snippet || ""
    };
  });
}

export async function searchWebInfo({ query, type }) {
  if (!query || !type) {
    throw new Error("searchWebInfo requires 'query' and 'type'.");
  }
  if (type !== "company" && type !== "person") {
    throw new Error(`Unknown search type: ${type}`);
  }

  const cacheKey = `searchWebInfo:${type}:${query.toLowerCase().trim()}`;
  return withCache(cacheKey, () => searchWebInfoImpl({ query, type }));
}

async function searchWebInfoImpl({ query, type }) {
  // Freshness intent → SerpAPI directly (Gemini's knowledge cutoff
  // makes it useless for "recent news / latest funding" queries).
  if (FRESHNESS_PATTERN.test(query)) {
    return serpWebInfo({ query, type });
  }

  // Otherwise, ask Gemini first. If it confidently knows the entity,
  // we save a SerpAPI call. If it returns _unknown or the call fails,
  // fall back to SerpAPI.
  try {
    const geminiResult = await geminiWebInfo({ query, type });
    if (geminiResult && !geminiResult._unknown) {
      return [geminiResult];
    }
  } catch (err) {
    console.warn(`[searchWebInfo] Gemini path failed, falling back to SerpAPI: ${err.message}`);
  }

  return serpWebInfo({ query, type });
}

async function geminiWebInfo({ query, type }) {
  const prompt = type === "company"
    ? `You are profiling the company "${query}".

If you have reliable information about this company in your training data, respond with ONLY this JSON:
{
  "title": "<company name and short tagline>",
  "snippet": "<2-3 sentence overview: what they do, size, sector>",
  "url": "<official website URL if known, else null>",
  "linkedInUrl": null,
  "source": "gemini",
  "recentNews": []
}

If you do NOT have reliable information about this specific company, respond with ONLY:
{"_unknown": true}

Do not invent facts. Do not guess LinkedIn URLs.`
    : `You are profiling the person "${query}".

If you have reliable information about this specific person in your training data (notable executives, public figures, etc.), respond with ONLY this JSON:
{
  "title": "<name - role at company>",
  "snippet": "<2-3 sentence professional summary>",
  "url": null,
  "linkedInUrl": null,
  "source": "gemini",
  "recentNews": []
}

If you do NOT have reliable information about this specific person, respond with ONLY:
{"_unknown": true}

Do not invent facts. Do not guess LinkedIn URLs.`;

  return geminiAskJson(prompt);
}

async function serpWebInfo({ query, type }) {
  // Hint the search toward LinkedIn / company profile pages: better
  // signal-to-noise than a bare keyword query. Quoting keeps multi-word
  // names intact.
  const hinted = type === "company"
    ? `"${query}" company profile`
    : `"${query}" linkedin`;

  const results = await serpSearch(hinted, { count: 5 });

  if (!results.length) {
    return [{
      title: `${query} - no results`,
      snippet: `Web search returned no results for "${query}".`,
      url: null,
      linkedInUrl: null,
      source: "none",
      recentNews: []
    }];
  }

  return results.map(r => {
    const isLinkedIn = /\blinkedin\.com\b/i.test(r.url);
    return {
      title: r.title,
      snippet: r.description,
      url: r.url,
      linkedInUrl: isLinkedIn ? r.url : null,
      source: isLinkedIn ? "linkedin" : "web",
      recentNews: []
    };
  });
}

export async function analyzeAttendeeBackground({ email }) {
  if (!email) throw new Error("analyzeAttendeeBackground requires 'email'.");
  const cacheKey = `analyzeAttendeeBackground:${email.toLowerCase().trim()}`;
  return withCache(cacheKey, () => analyzeAttendeeBackgroundImpl({ email }));
}

async function analyzeAttendeeBackgroundImpl({ email }) {
  // Derive a name + company from the email — used either as the final
  // result (internal attendees) or as the search query (external).
  const localPart = (email.split("@")[0] || email).replace(/\./g, " ");
  const domain    = email.split("@")[1] || "unknown";
  const name      = capitalize(localPart);
  const company   = capitalize(domain.split(".")[0]);

  // Internal attendee: skip both APIs entirely. The system prompt
  // already discourages researching colleagues; this enforces it
  // and saves quota when the agent calls the tool anyway.
  if (isOwnAttendee(email)) {
    return {
      name,
      currentRole: null,
      company,
      background: `Internal teammate (${email}). Skipped external research per policy — refer to your own people directory for details.`,
      linkedInUrl: null,
      sources: [],
      _internal: true
    };
  }

  // External attendee: SerpAPI is the only reliable source for the
  // LinkedIn URL (Gemini hallucinates URLs), so we always make that
  // call. Gemini then synthesizes role + background from the snippets.
  const results = await serpSearch(`"${name}" "${company}" linkedin`, { count: 5 });

  if (!results.length) {
    return {
      name,
      currentRole: null,
      company,
      background: `No public information found for ${email}.`,
      linkedInUrl: null,
      sources: []
    };
  }

  const linkedIn = findLinkedInResult(results);

  // Gemini synthesis from the SerpAPI snippets. If Gemini fails
  // (no key, API down), fall back to concatenating the top snippets
  // — the agent's LLM can still read them.
  let synthesized = null;
  try {
    synthesized = await geminiSynthesizeProfile({ name, company, results });
  } catch (err) {
    console.warn(`[analyzeAttendeeBackground] Gemini synthesis failed, using raw snippets: ${err.message}`);
  }

  const fallbackBackground = results
    .slice(0, 2)
    .map(r => r.description)
    .filter(Boolean)
    .join(" ");

  return {
    name,
    currentRole: synthesized?.currentRole || null,
    company,
    background:
      synthesized?.background ||
      fallbackBackground ||
      `Search returned matches but no readable snippets for ${email}.`,
    linkedInUrl: linkedIn?.url || null,
    sources: results.map(r => ({ title: r.title, url: r.url, snippet: r.description }))
  };
}

async function geminiSynthesizeProfile({ name, company, results }) {
  const snippetText = results
    .map((r, i) => `[${i + 1}] ${r.title}\n${r.description}\nURL: ${r.url}`)
    .join("\n\n");

  const prompt = `You are profiling a meeting attendee strictly from web search results.

Attendee: "${name}"
Email-derived company: "${company}"

Search results:
${snippetText}

Respond with ONLY this JSON:
{
  "currentRole": "<their current job title as stated in the snippets, else null>",
  "background": "<2-3 sentence professional summary based ONLY on the snippets above. Do not invent facts.>"
}

If the snippets clearly do not refer to this person, set both fields to null.`;

  return geminiAskJson(prompt);
}

function capitalize(s) {
  return s.replace(/\b\w/g, c => c.toUpperCase());
}

export async function calculateMeetingStats({ meetings, hoursAhead, timeframe, userTimeZone }) {
  // Two input modes:
  //   (a) meetings supplied → compute over that exact set
  //   (b) meetings omitted  → fetch from Calendar using hoursAhead
  //                            (or a sensible default derived from timeframe)
  // Mode (b) is the preferred path for "what's my meeting load this week?"
  // — it skips having to round-trip the meetings array through Gemini,
  // which can blow the output token budget for large weeks and trigger
  // MALFORMED_FUNCTION_CALL.
  if (!Array.isArray(meetings)) {
    const hours = typeof hoursAhead === "number" && hoursAhead > 0
      ? hoursAhead
      : (TIMEFRAME_HOURS[timeframe] ?? 168);
    meetings = await getUpcomingMeetings({ hoursAhead: hours });
  }

  const days = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];

  // Compute day-of-week in the user's local TZ via Intl. Without this,
  // start.getDay() returns the day in the server's TZ, which can attribute
  // a Friday-evening-CST meeting to Saturday-UTC and skew the per-day
  // breakdown. userTimeZone is auto-injected by mcp-client.js; if it's
  // missing for any reason, we fall back to server-local (still better
  // than failing).
  const dayFormatter = new Intl.DateTimeFormat("en-US", {
    weekday: "long",
    timeZone: userTimeZone || undefined
  });

  const distribution = {};
  const hoursByDay = {};
  const meetingsByDay = {};
  for (const d of days) {
    distribution[d]  = 0;
    hoursByDay[d]    = 0;
    meetingsByDay[d] = [];
  }

  // Multi-day events (e.g. 3-day off-sites, multi-day all-day blocks)
  // distort meeting-load math when attributed to a single day at full
  // duration. We exclude anything that spans more than one calendar
  // day; single-day events including all-day (24h) ones still count.
  const MULTI_DAY_THRESHOLD_MS = 24 * 3600 * 1000;

  let totalMs = 0;
  let included = 0;
  let excludedMultiDay = 0;

  for (const m of meetings) {
    const start = new Date(m.startTime);
    const end   = new Date(m.endTime);
    const durMs = end - start;
    if (!Number.isFinite(durMs) || durMs <= 0) continue;

    if (durMs >= MULTI_DAY_THRESHOLD_MS) {
      excludedMultiDay++;
      continue;
    }

    const day = dayFormatter.format(start);   // "Monday", "Tuesday", etc.
    if (!days.includes(day)) continue;        // defensive: unexpected locale output

    totalMs += durMs;
    included++;
    distribution[day]++;
    hoursByDay[day] += durMs / 3600000;
    meetingsByDay[day].push({
      id: m.id,
      title: m.title,
      startTime: m.startTime,
      endTime: m.endTime,
      durationMinutes: Math.round(durMs / 60000),
      location: m.location || null
    });
  }

  for (const d of days) {
    hoursByDay[d] = +hoursByDay[d].toFixed(2);
    meetingsByDay[d].sort((a, b) => new Date(a.startTime) - new Date(b.startTime));
  }

  const loadByDay = {};
  for (const d of days) {
    const h = hoursByDay[d];
    if (h === 0)      loadByDay[d] = "free";
    else if (h <= 1.5) loadByDay[d] = "light";
    else if (h <= 3)   loadByDay[d] = "medium";
    else if (h <= 5)   loadByDay[d] = "heavy";
    else               loadByDay[d] = "packed";
  }

  // Surface the TZ used for day-of-week so the agent (and the brief)
  // can label the report unambiguously.
  const reportTimeZone = userTimeZone || dayFormatter.resolvedOptions().timeZone;

  if (included === 0) {
    return {
      timeframe: timeframe || "n/a",
      timeZone: reportTimeZone,
      totalMeetings: 0,
      totalHours: 0,
      averageDurationHours: 0,
      busiestDay: null,
      excludedMultiDay,
      meetingDistribution: distribution,
      hoursByDay,
      loadByDay,
      meetingsByDay
    };
  }

  const totalHours = +(totalMs / 3600000).toFixed(2);
  const averageDurationHours = +(totalHours / included).toFixed(2);
  const busiestDay = Object.entries(distribution).sort((a, b) => b[1] - a[1])[0][0];

  return {
    timeframe: timeframe || "n/a",
    timeZone: reportTimeZone,
    totalMeetings: included,
    totalHours,
    averageDurationHours,
    busiestDay,
    excludedMultiDay,
    meetingDistribution: distribution,
    hoursByDay,
    loadByDay,
    meetingsByDay
  };
}
