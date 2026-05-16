"""Tool implementations backing the MCP server (Python rewrite).

Backends per tool:
  getUpcomingMeetings        → Google Calendar
  searchGmail                → Gmail API
  searchWebInfo              → Gemini first, SerpAPI fallback
                               (SerpAPI directly when query has freshness intent)
  analyzeAttendeeBackground  → 0 calls for internal attendees
                               else SerpAPI (LinkedIn URL) + Gemini (synthesis)
  calculateMeetingStats      → pure computation

All external lookups are wrapped in a process-local LRU (cache.py) so
repeat queries within a popup session pay nothing.

Each tool returns a JSON-serializable dict (built from a Pydantic model
via .model_dump(by_alias=True)) so the MCP layer can wrap it in a
text-content block exactly the way the extension already expects.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from cache import with_cache
from google_auth import get_authorized_credentials
from llm import gemini_ask_json
from models import (
    AnalyzeAttendeeInput,
    AttendeeProfile,
    AttendeeSource,
    CalculateMeetingStatsInput,
    DayMeeting,
    EmailHit,
    GetUpcomingMeetingsInput,
    Meeting,
    MeetingStats,
    SearchGmailInput,
    SearchWebInfoInput,
    WebInfoResult,
)
from serpapi import SerpResult, find_linkedin_result, serp_search

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Default lookahead per timeframe label, used by calculate_meeting_stats
# when the agent passes only `timeframe` (no explicit hoursAhead, no
# meetings array).
TIMEFRAME_HOURS = {"today": 24.0, "week": 168.0, "month": 720.0}

# Phrases that indicate the user wants fresh, post-cutoff information.
# When the query matches, we skip Gemini (knowledge-cutoff Jan 2026)
# and go straight to SerpAPI.
_FRESHNESS_RE = re.compile(
    r"\b(news|recent|latest|today|yesterday|this\s+(week|month|quarter|year)|"
    r"current|now|funding|announce(d|ment)?|just\s+launched|2026|2027|2028)\b",
    re.IGNORECASE,
)

DAY_ORDER = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def _get_own_domains() -> list[str]:
    """Comma-separated list of email domains treated as 'internal'."""
    raw = os.environ.get("OWN_COMPANY_DOMAIN") or ""
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _is_own_attendee(email: str) -> bool:
    domains = _get_own_domains()
    if not domains:
        return False
    _, _, domain = email.partition("@")
    return domain.lower() in domains


# ----------------------------------------------------------------------
# getUpcomingMeetings
# ----------------------------------------------------------------------


def _end_of_day_in_tz(now: datetime, tz_name: str) -> datetime:
    """Returns a UTC datetime representing the end of the current calendar
    day in the given IANA timezone."""
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        return now + timedelta(hours=24)
    local_now = now.astimezone(zone)
    local_eod = local_now.replace(hour=23, minute=59, second=59, microsecond=999_999)
    return local_eod.astimezone(timezone.utc)


def _normalize_event_time(t: dict | None) -> str | None:
    """Calendar API returns either start.dateTime (ISO with offset) for timed
    events or start.date (YYYY-MM-DD) for all-day events. Keep timed
    events as-is so the offset is preserved; synthesize UTC midnight for
    all-day events so callers can still parse them as datetimes."""
    if not t:
        return None
    if t.get("dateTime"):
        return t["dateTime"]
    if t.get("date"):
        return f"{t['date']}T00:00:00Z"
    return None


def _is_all_day_event(start: dict | None) -> bool:
    """Calendar API uses start.date (no time) exclusively for all-day events.
    We use this to flag them so calculate_meeting_stats can exclude them
    from hour totals — otherwise a single PTO/holiday marker contributes
    a full 24h to the day's 'meeting load'."""
    if not start:
        return False
    return bool(start.get("date")) and not start.get("dateTime")


async def get_upcoming_meetings(args: dict[str, Any]) -> list[dict]:
    parsed = GetUpcomingMeetingsInput.model_validate(args)
    creds = await get_authorized_credentials(CALENDAR_SCOPES)

    now = datetime.now(timezone.utc)
    if parsed.end_of_today and parsed.user_time_zone:
        cutoff = _end_of_day_in_tz(now, parsed.user_time_zone)
    else:
        cutoff = now + timedelta(hours=parsed.hours_ahead)

    def _list() -> dict:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat().replace("+00:00", "Z"),
                timeMax=cutoff.isoformat().replace("+00:00", "Z"),
                singleEvents=True,  # expand recurring events into individual instances
                orderBy="startTime",
                maxResults=50,
            )
            .execute()
        )

    res = await asyncio.to_thread(_list)
    items = res.get("items") or []

    meetings: list[Meeting] = []
    for ev in items:
        if ev.get("status") == "cancelled":
            continue
        start = _normalize_event_time(ev.get("start"))
        end = _normalize_event_time(ev.get("end"))
        if not start:
            continue
        attendees = [
            a["email"]
            for a in (ev.get("attendees") or [])
            if a.get("email") and not a.get("self") and not a.get("resource")
        ]
        meetings.append(
            Meeting(
                id=ev.get("id"),
                title=ev.get("summary") or "(no title)",
                startTime=start,
                endTime=end,
                timeZone=(ev.get("start") or {}).get("timeZone")
                or (ev.get("end") or {}).get("timeZone"),
                attendees=attendees,
                location=ev.get("location") or None,
                description=ev.get("description") or "",
                allDay=_is_all_day_event(ev.get("start")),
            )
        )
    return [m.model_dump() for m in meetings]


# ----------------------------------------------------------------------
# searchGmail
# ----------------------------------------------------------------------


async def search_gmail(args: dict[str, Any]) -> list[dict]:
    parsed = SearchGmailInput.model_validate(args)
    creds = await get_authorized_credentials(GMAIL_SCOPES)

    def _list_ids() -> list[str]:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        res = (
            service.users()
            .messages()
            .list(userId="me", q=parsed.query, maxResults=parsed.max_results)
            .execute()
        )
        return [m["id"] for m in (res.get("messages") or [])]

    ids = await asyncio.to_thread(_list_ids)
    if not ids:
        return []

    def _get_one(message_id: str) -> dict:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            )
            .execute()
        )

    # Parallel fetch of message metadata.
    messages = await asyncio.gather(*(asyncio.to_thread(_get_one, i) for i in ids))

    out: list[EmailHit] = []
    for m in messages:
        headers = (m.get("payload") or {}).get("headers") or []
        header_map = {h.get("name", "").lower(): h.get("value", "") for h in headers}

        date_raw = header_map.get("date", "")
        date = ""
        if date_raw:
            try:
                # Gmail dates are RFC 2822; parse via email.utils for robustness.
                from email.utils import parsedate_to_datetime

                date = parsedate_to_datetime(date_raw).date().isoformat()
            except Exception:
                date = ""

        out.append(
            EmailHit(
                subject=header_map.get("subject") or "(no subject)",
                **{"from": header_map.get("from", "")},
                date=date,
                snippet=m.get("snippet") or "",
            )
        )
    return [hit.model_dump(by_alias=True) for hit in out]


# ----------------------------------------------------------------------
# searchWebInfo  (Gemini-first, SerpAPI fallback)
# ----------------------------------------------------------------------


async def _gemini_web_info(query: str, kind: str) -> dict | None:
    if kind == "company":
        prompt = f"""You are profiling the company "{query}".

If you have reliable information about this company in your training data, respond with ONLY this JSON:
{{
  "title": "<company name and short tagline>",
  "snippet": "<2-3 sentence overview: what they do, size, sector>",
  "url": "<official website URL if known, else null>",
  "linkedInUrl": null,
  "source": "gemini",
  "recentNews": []
}}

If you do NOT have reliable information about this specific company, respond with ONLY:
{{"_unknown": true}}

Do not invent facts. Do not guess LinkedIn URLs."""
    else:
        prompt = f"""You are profiling the person "{query}".

If you have reliable information about this specific person in your training data (notable executives, public figures, etc.), respond with ONLY this JSON:
{{
  "title": "<name - role at company>",
  "snippet": "<2-3 sentence professional summary>",
  "url": null,
  "linkedInUrl": null,
  "source": "gemini",
  "recentNews": []
}}

If you do NOT have reliable information about this specific person, respond with ONLY:
{{"_unknown": true}}

Do not invent facts. Do not guess LinkedIn URLs."""

    return await gemini_ask_json(prompt)


async def _serp_web_info(query: str, kind: str) -> list[WebInfoResult]:
    # Hint the search toward LinkedIn / company profile pages.
    hinted = f'"{query}" company profile' if kind == "company" else f'"{query}" linkedin'
    results = await serp_search(hinted, count=5)

    if not results:
        return [
            WebInfoResult(
                title=f"{query} - no results",
                snippet=f'Web search returned no results for "{query}".',
                url=None,
                linkedInUrl=None,
                source="none",
                recentNews=[],
            )
        ]

    out: list[WebInfoResult] = []
    for r in results:
        is_li = "linkedin.com" in r.url.lower()
        out.append(
            WebInfoResult(
                title=r.title,
                snippet=r.description,
                url=r.url,
                linkedInUrl=r.url if is_li else None,
                source="linkedin" if is_li else "web",
                recentNews=[],
            )
        )
    return out


async def search_web_info(args: dict[str, Any]) -> list[dict]:
    parsed = SearchWebInfoInput.model_validate(args)
    cache_key = f"searchWebInfo:{parsed.type}:{parsed.query.strip().lower()}"

    async def _impl() -> list[dict]:
        # Freshness intent → SerpAPI directly.
        if _FRESHNESS_RE.search(parsed.query):
            return [r.model_dump(by_alias=True, exclude={"unknown_"})
                    for r in await _serp_web_info(parsed.query, parsed.type)]

        # Otherwise, ask Gemini first; fall back to SerpAPI if it doesn't
        # know the entity or the call fails.
        try:
            gemini_raw = await _gemini_web_info(parsed.query, parsed.type)
            if gemini_raw and not gemini_raw.get("_unknown"):
                # Validate / normalize the Gemini answer through the model.
                validated = WebInfoResult.model_validate(
                    {**gemini_raw, "source": gemini_raw.get("source", "gemini")}
                )
                return [validated.model_dump(by_alias=True, exclude={"unknown_"})]
        except Exception as exc:
            print(
                f"[search_web_info] Gemini path failed, falling back to SerpAPI: {exc}",
                flush=True,
            )

        return [
            r.model_dump(by_alias=True, exclude={"unknown_"})
            for r in await _serp_web_info(parsed.query, parsed.type)
        ]

    return await with_cache(cache_key, _impl)


# ----------------------------------------------------------------------
# analyzeAttendeeBackground
# ----------------------------------------------------------------------


def _capitalize_words(s: str) -> str:
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), s)


async def _gemini_synthesize_profile(
    *, name: str, company: str, results: list[SerpResult]
) -> dict | None:
    snippet_text = "\n\n".join(
        f"[{i+1}] {r.title}\n{r.description}\nURL: {r.url}" for i, r in enumerate(results)
    )
    prompt = f"""You are profiling a meeting attendee strictly from web search results.

Attendee: "{name}"
Email-derived company: "{company}"

Search results:
{snippet_text}

Respond with ONLY this JSON:
{{
  "currentRole": "<their current job title as stated in the snippets, else null>",
  "background": "<2-3 sentence professional summary based ONLY on the snippets above. Do not invent facts.>"
}}

If the snippets clearly do not refer to this person, set both fields to null."""
    return await gemini_ask_json(prompt)


async def analyze_attendee_background(args: dict[str, Any]) -> dict:
    parsed = AnalyzeAttendeeInput.model_validate(args)
    email = parsed.email
    cache_key = f"analyzeAttendeeBackground:{email.strip().lower()}"

    async def _impl() -> dict:
        local_part = email.split("@", 1)[0].replace(".", " ")
        domain = email.split("@", 1)[1] if "@" in email else "unknown"
        name = _capitalize_words(local_part)
        company = _capitalize_words(domain.split(".", 1)[0])

        if _is_own_attendee(email):
            profile = AttendeeProfile(
                name=name,
                currentRole=None,
                company=company,
                background=(
                    f"Internal teammate ({email}). Skipped external research per policy — "
                    "refer to your own people directory for details."
                ),
                linkedInUrl=None,
                sources=[],
                **{"_internal": True},
            )
            return profile.model_dump(by_alias=True)

        results = await serp_search(f'"{name}" "{company}" linkedin', count=5)
        if not results:
            return AttendeeProfile(
                name=name,
                currentRole=None,
                company=company,
                background=f"No public information found for {email}.",
                linkedInUrl=None,
                sources=[],
            ).model_dump(by_alias=True)

        linkedin = find_linkedin_result(results)

        synthesized: dict | None = None
        try:
            synthesized = await _gemini_synthesize_profile(
                name=name, company=company, results=results
            )
        except Exception as exc:
            print(
                f"[analyze_attendee_background] Gemini synthesis failed, "
                f"using raw snippets: {exc}",
                flush=True,
            )

        fallback_bg = " ".join(
            r.description for r in results[:2] if r.description
        ).strip()

        profile = AttendeeProfile(
            name=name,
            currentRole=(synthesized or {}).get("currentRole"),
            company=company,
            background=(
                (synthesized or {}).get("background")
                or fallback_bg
                or f"Search returned matches but no readable snippets for {email}."
            ),
            linkedInUrl=linkedin.url if linkedin else None,
            sources=[
                AttendeeSource(title=r.title, url=r.url, snippet=r.description) for r in results
            ],
        )
        return profile.model_dump(by_alias=True)

    return await with_cache(cache_key, _impl)


# ----------------------------------------------------------------------
# calculateMeetingStats
# ----------------------------------------------------------------------


async def calculate_meeting_stats(args: dict[str, Any]) -> dict:
    parsed = CalculateMeetingStatsInput.model_validate(args)

    # Two input modes:
    #   (a) meetings supplied → compute over that exact set
    #   (b) meetings omitted  → fetch from Calendar using hoursAhead
    #                            (or a default derived from timeframe)
    if parsed.meetings is None:
        hours = parsed.hours_ahead if (parsed.hours_ahead and parsed.hours_ahead > 0) else (
            TIMEFRAME_HOURS.get(parsed.timeframe or "", 168.0)
        )
        fetched = await get_upcoming_meetings({"hoursAhead": hours})
        meetings_iter: list[dict] = fetched
    else:
        meetings_iter = [m.model_dump() for m in parsed.meetings]

    # Compute day-of-week in the user's local TZ. Without this, the day
    # bucket is computed in the server's zone, which can attribute a
    # Friday-evening-CST meeting to Saturday-UTC and skew the breakdown.
    tz: ZoneInfo | None
    try:
        tz = ZoneInfo(parsed.user_time_zone) if parsed.user_time_zone else None
    except Exception:
        tz = None
    fallback_tz = parsed.user_time_zone or "UTC"

    distribution = {d: 0 for d in DAY_ORDER}
    hours_by_day = {d: 0.0 for d in DAY_ORDER}
    meetings_by_day: dict[str, list[DayMeeting]] = {d: [] for d in DAY_ORDER}

    # Multi-day events distort meeting-load math when attributed to a
    # single day at full duration. Exclude anything that spans more than
    # one calendar day (>24 h). All-day events (PTO / holiday / OOO
    # markers shown as date-only) are also excluded — they're not
    # "meetings" in the load sense, but a single one would contribute
    # 24h to the day total and dwarf real meetings.
    multi_day_threshold_seconds = 24 * 3600

    total_seconds = 0.0
    included = 0
    excluded_multi_day = 0
    excluded_all_day = 0

    for m in meetings_iter:
        start_iso = m.get("startTime")
        end_iso = m.get("endTime")
        if not start_iso or not end_iso:
            continue
        try:
            start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        dur_seconds = (end_dt - start_dt).total_seconds()
        if dur_seconds <= 0:
            continue

        # All-day exclusion. Two paths:
        #  (a) explicit flag from get_upcoming_meetings / agent input;
        #  (b) heuristic: start time exactly matches the all-day shape
        #      produced by _normalize_event_time (`T00:00:00Z`) AND the
        #      duration is a clean multiple of 24h. Catches cases where
        #      the agent passed a meetings array without the allDay
        #      field set.
        is_all_day = bool(m.get("allDay")) or (
            start_iso.endswith("T00:00:00Z")
            and end_iso.endswith("T00:00:00Z")
            and dur_seconds % multi_day_threshold_seconds == 0
        )
        if is_all_day:
            excluded_all_day += 1
            continue

        if dur_seconds > multi_day_threshold_seconds:
            excluded_multi_day += 1
            continue

        local_start = start_dt.astimezone(tz) if tz else start_dt
        day_name = DAY_ORDER[(local_start.weekday() + 1) % 7]  # Mon=0 → "Monday" index 1

        total_seconds += dur_seconds
        included += 1
        distribution[day_name] += 1
        hours_by_day[day_name] += dur_seconds / 3600.0
        meetings_by_day[day_name].append(
            DayMeeting(
                id=m.get("id"),
                title=m.get("title"),
                startTime=start_iso,
                endTime=end_iso,
                durationMinutes=round(dur_seconds / 60.0),
                location=m.get("location"),
            )
        )

    for d in DAY_ORDER:
        hours_by_day[d] = round(hours_by_day[d], 2)
        meetings_by_day[d].sort(key=lambda x: x.startTime)

    load_by_day: dict[str, str] = {}
    for d in DAY_ORDER:
        h = hours_by_day[d]
        if h == 0:
            load_by_day[d] = "free"
        elif h <= 1.5:
            load_by_day[d] = "light"
        elif h <= 3:
            load_by_day[d] = "medium"
        elif h <= 5:
            load_by_day[d] = "heavy"
        else:
            load_by_day[d] = "packed"

    report_tz = parsed.user_time_zone or fallback_tz

    if included == 0:
        stats = MeetingStats(
            timeframe=parsed.timeframe or "n/a",
            timeZone=report_tz,
            totalMeetings=0,
            totalHours=0.0,
            averageDurationHours=0.0,
            busiestDay=None,
            excludedMultiDay=excluded_multi_day,
            excludedAllDay=excluded_all_day,
            meetingDistribution=distribution,
            hoursByDay=hours_by_day,
            loadByDay=load_by_day,
            meetingsByDay={
                d: [m.model_dump() for m in meetings_by_day[d]] for d in DAY_ORDER
            },
        )
        return stats.model_dump()

    total_hours = round(total_seconds / 3600.0, 2)
    average_duration_hours = round(total_hours / included, 2)
    busiest_day = max(distribution.items(), key=lambda kv: kv[1])[0]

    stats = MeetingStats(
        timeframe=parsed.timeframe or "n/a",
        timeZone=report_tz,
        totalMeetings=included,
        totalHours=total_hours,
        averageDurationHours=average_duration_hours,
        busiestDay=busiest_day,
        excludedMultiDay=excluded_multi_day,
        excludedAllDay=excluded_all_day,
        meetingDistribution=distribution,
        hoursByDay=hours_by_day,
        loadByDay=load_by_day,
        meetingsByDay={d: [m.model_dump() for m in meetings_by_day[d]] for d in DAY_ORDER},
    )
    return stats.model_dump()
