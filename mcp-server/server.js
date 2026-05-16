// Builds an McpServer instance with the 5 meeting-intelligence tools
// registered. Stateless: index.js calls createServer() per request.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import * as h from "./handlers.js";

export function createServer() {
  const server = new McpServer({
    name: "meeting-intelligence",
    version: "0.1.0"
  });

  server.registerTool(
    "getUpcomingMeetings",
    {
      title: "Get upcoming meetings",
      description:
        "Fetches the user's upcoming meetings from their calendar within a time window. " +
        "Use this first when the user asks about meetings, schedule, or wants to prepare for upcoming events.",
      inputSchema: {
        hoursAhead: z.number().optional().describe("How many hours ahead to look. Default 24."),
        endOfToday: z.boolean().optional().describe(
          "When true, fetch only meetings through the end of today in the user's local timezone. " +
          "Use this instead of hoursAhead when the user asks about 'today'."
        ),
        userTimeZone: z.string().optional().describe(
          "IANA timezone name (e.g. 'America/Chicago'). Auto-injected by the MCP client — do not set this yourself."
        )
      }
    },
    async (args) => textResult(await h.getUpcomingMeetings(args))
  );

  server.registerTool(
    "searchGmail",
    {
      title: "Search Gmail",
      description:
        "Searches the user's email for messages matching a query. " +
        "Use this to find prior context (threads, attachments, prior commitments) about a meeting, person, or company.",
      inputSchema: {
        query: z.string().describe("Free-text search keywords (e.g. company name, person name, project)."),
        maxResults: z.number().optional().describe("Max number of email snippets to return. Default 5.")
      }
    },
    async (args) => textResult(await h.searchGmail(args))
  );

  server.registerTool(
    "searchWebInfo",
    {
      title: "Search web for company/person info",
      description:
        "Searches the web for public information about a company or a person. " +
        "Use this to gather background, recent news, funding, or product context that is NOT in the user's email/calendar.",
      inputSchema: {
        query: z.string().describe("What to search for (e.g. company name)."),
        type: z.enum(["company", "person"]).describe("Whether the query targets a company or a person.")
      }
    },
    async (args) => textResult(await h.searchWebInfo(args))
  );

  server.registerTool(
    "analyzeAttendeeBackground",
    {
      title: "Analyze attendee background",
      description:
        "Looks up the professional background of a meeting attendee (role, company, work history). " +
        "Use this once you know who is attending a meeting and want a quick profile.",
      inputSchema: {
        email: z.string().describe("Email address of the attendee.")
      }
    },
    async (args) => textResult(await h.analyzeAttendeeBackground(args))
  );

  server.registerTool(
    "calculateMeetingStats",
    {
      title: "Calculate meeting statistics",
      description:
        "Computes statistics over a meeting set (total count, total hours, busiest day, distribution). " +
        "PREFERRED USAGE for 'meeting load' / schedule-analysis queries: pass only `hoursAhead` " +
        "(e.g. 24 for today, 168 for a week, 720 for a month) and the tool fetches the calendar " +
        "itself — no need to first call getUpcomingMeetings and re-pass the meetings array. " +
        "ALTERNATIVE: pass an explicit `meetings` array if you want stats over a curated subset.",
      inputSchema: {
        hoursAhead: z.number().optional().describe(
          "Time window to fetch meetings for, in hours. Used when `meetings` is not provided. " +
          "Defaults: today=24, week=168, month=720, otherwise 168."
        ),
        timeframe: z.enum(["today", "week", "month"]).optional()
          .describe("Human-readable label; also picks the default hoursAhead when neither is set."),
        meetings: z.array(z.object({
          startTime: z.string().describe("ISO timestamp with offset, e.g. 2026-05-03T14:00:00-05:00"),
          endTime:   z.string().describe("ISO timestamp with offset"),
          id:        z.string().optional(),
          title:     z.string().optional(),
          location:  z.string().nullable().optional()
        })).optional().describe(
          "Optional explicit meeting set. Only use this when stats over a curated subset are needed; " +
          "for whole-week/month queries pass hoursAhead instead so the array doesn't have to be " +
          "re-serialized through the LLM."
        ),
        userTimeZone: z.string().optional().describe(
          "IANA timezone name (e.g. 'America/Chicago') used to compute day-of-week. " +
          "Auto-injected by the MCP client from the user's browser — do not set this yourself."
        )
      }
    },
    async (args) => textResult(await h.calculateMeetingStats(args))
  );

  return server;
}

// MCP tool results are an array of content blocks. Our tools all return
// JSON, so we serialize once into a single text block. The extension's
// MCP client parses it back into an object before handing to the agent.
function textResult(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload) }]
  };
}
