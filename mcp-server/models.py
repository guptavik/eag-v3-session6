"""Pydantic v2 models for tool inputs and outputs.

Every MCP tool registered in `server.py` declares its arguments via these
input models and returns one of the output models below (serialized to
JSON before being wrapped in the MCP text-content block).

Field descriptions appear in the tool schema the agent sees — the
extension's LLM relies on them to know when to use each tool and which
arguments mean what. Keep them concrete.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# -------------------- shared --------------------


class CamelInputBase(BaseModel):
    """Base for tool-input models.

    The Chrome extension's mcp-client.js auto-injects `userTimeZone` (camelCase)
    into every tools/call invocation. The original Node server used Zod with
    camelCase keys natively; this Python port keeps the wire format identical
    by using Pydantic field aliases. The override below forces FastMCP's
    schema generation to use those aliases (Pydantic v2's default is to emit
    the Python field names, which would be snake_case and silently drop the
    auto-injected camelCase keys via `extra="ignore"`).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("by_alias", True)
        return super().model_json_schema(*args, **kwargs)


class StrictModel(BaseModel):
    """Tool *outputs* should be strict — extra fields are a bug, not noise."""

    model_config = ConfigDict(extra="forbid")


# -------------------- getUpcomingMeetings --------------------


class GetUpcomingMeetingsInput(CamelInputBase):
    """Inputs are lax: the agent may send extras (like the auto-injected
    `userTimeZone` from the client) that don't apply to every tool. Pydantic
    silently drops them — same behavior as Zod's default `.strip()` mode."""

    hours_ahead: float = Field(
        24, alias="hoursAhead", description="How many hours ahead to look. Default 24."
    )
    end_of_today: bool = Field(
        False,
        alias="endOfToday",
        description=(
            "When true, fetch only meetings through the end of today in the user's local timezone. "
            "Use this instead of hoursAhead when the user asks about 'today'."
        ),
    )
    user_time_zone: str | None = Field(
        None,
        alias="userTimeZone",
        description=(
            "IANA timezone name (e.g. 'America/Chicago'). Auto-injected by the MCP client — "
            "do not set this yourself."
        ),
    )


class Meeting(StrictModel):
    id: str | None = None
    title: str
    # Preserve the original ISO with offset (e.g. "2026-05-03T14:00:00-05:00")
    # so downstream consumers can convert to the user's local TZ without
    # losing information.
    startTime: str
    endTime: str | None = None
    timeZone: str | None = None
    attendees: list[str] = Field(default_factory=list)
    location: str | None = None
    description: str = ""
    # True when the calendar entry is an all-day event (had start.date,
    # no start.dateTime). Stats math must exclude these from hour totals —
    # otherwise a single OOO/holiday marker contributes a full 24h to the
    # day's "meeting load".
    allDay: bool = False


# -------------------- searchGmail --------------------


class SearchGmailInput(CamelInputBase):
    query: str = Field(
        ..., description="Free-text search keywords (e.g. company name, person name, project)."
    )
    max_results: int = Field(
        5,
        alias="maxResults",
        description="Max number of email snippets to return. Default 5.",
        ge=1,
        le=20,
    )


class EmailHit(StrictModel):
    subject: str
    from_: str = Field(..., alias="from", serialization_alias="from")
    date: str
    snippet: str

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# -------------------- searchWebInfo --------------------


class SearchWebInfoInput(CamelInputBase):
    query: str = Field(..., description="What to search for (e.g. company name).")
    type: Literal["company", "person"] = Field(
        ..., description="Whether the query targets a company or a person."
    )


class WebInfoResult(StrictModel):
    title: str
    snippet: str
    url: str | None = None
    linkedInUrl: str | None = None
    source: Literal["gemini", "linkedin", "web", "none"]
    recentNews: list[str] = Field(default_factory=list)
    # Set when Gemini was asked but didn't know the entity. Triggers a
    # SerpAPI fallback in the handler.
    unknown_: bool = Field(False, alias="_unknown")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# -------------------- analyzeAttendeeBackground --------------------


class AnalyzeAttendeeInput(CamelInputBase):
    email: str = Field(..., description="Email address of the attendee.")


class AttendeeSource(StrictModel):
    title: str
    url: str
    snippet: str


class AttendeeProfile(StrictModel):
    name: str
    currentRole: str | None = None
    company: str
    background: str
    linkedInUrl: str | None = None
    sources: list[AttendeeSource] = Field(default_factory=list)
    # True for OWN_COMPANY_DOMAIN attendees — set so the agent can branch on
    # internal vs external without re-parsing the email domain.
    internal_: bool = Field(False, alias="_internal")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# -------------------- calculateMeetingStats --------------------


class StatsMeetingInput(CamelInputBase):
    """Subset of Meeting the agent can pass in the optional `meetings`
    array. We keep it minimal so the LLM doesn't have to re-serialize the
    whole Meeting shape."""

    startTime: str = Field(..., description="ISO timestamp with offset, e.g. 2026-05-03T14:00:00-05:00")
    endTime: str = Field(..., description="ISO timestamp with offset")
    id: str | None = None
    title: str | None = None
    location: str | None = None
    allDay: bool = Field(
        False,
        description="True for all-day calendar entries (OOO / holiday markers). Excluded from hour totals.",
    )


class CalculateMeetingStatsInput(CamelInputBase):
    hours_ahead: float | None = Field(
        None,
        alias="hoursAhead",
        description=(
            "Time window to fetch meetings for, in hours. Used when `meetings` is not provided. "
            "Defaults: today=24, week=168, month=720, otherwise 168."
        ),
    )
    timeframe: Literal["today", "week", "month"] | None = Field(
        None,
        description="Human-readable label; also picks the default hoursAhead when neither is set.",
    )
    meetings: list[StatsMeetingInput] | None = Field(
        None,
        description=(
            "Optional explicit meeting set. Only use this when stats over a curated subset are needed; "
            "for whole-week/month queries pass hoursAhead instead so the array doesn't have to be "
            "re-serialized through the LLM."
        ),
    )
    user_time_zone: str | None = Field(
        None,
        alias="userTimeZone",
        description=(
            "IANA timezone name (e.g. 'America/Chicago') used to compute day-of-week. "
            "Auto-injected by the MCP client from the user's browser — do not set this yourself."
        ),
    )


class DayMeeting(StrictModel):
    id: str | None = None
    title: str | None = None
    startTime: str
    endTime: str
    durationMinutes: int
    location: str | None = None


class MeetingStats(StrictModel):
    timeframe: str
    timeZone: str
    totalMeetings: int
    totalHours: float
    averageDurationHours: float
    busiestDay: str | None
    excludedMultiDay: int
    # All-day calendar entries (OOO, holidays, multi-day blocks shown as
    # date-only) skipped from hour totals. Reported separately so the
    # agent can mention them in the brief without inflating "meeting
    # load" numbers — a single OOO marker would otherwise contribute
    # 24h to the day's load.
    excludedAllDay: int = 0
    meetingDistribution: dict[str, int]
    hoursByDay: dict[str, float]
    loadByDay: dict[str, str]
    meetingsByDay: dict[str, list[DayMeeting]]
