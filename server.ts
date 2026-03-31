#!/usr/bin/env npx tsx
/**
 * cc MCP server — native tools for multi-session awareness.
 *
 * Gives Claude cc_peers, cc_roster, and cc_send as first-class tools.
 * Uses /tmp/claude-{uid}/ for liveness, ~/.claude/cc/ for metadata and mailbox.
 * File locking via lockfile pattern (atomic rename) for concurrent safety.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

// Paths
const CC_DIR = path.join(os.homedir(), ".claude", "cc");
const TEAMS_DIR = path.join(CC_DIR, "teams");
const MAILBOX_DIR = path.join(CC_DIR, "mailbox");
const TMP_BASE = path.join("/tmp", `claude-${process.getuid!()}`);

// Types matching Anthropic's TeammateMessage schema
type TeamMember = {
  agentId: string;
  name: string;
  cwd: string;
  branch: string;
  files: string[];
  task: string;
  isActive: boolean;
  joinedAt: number;
};

type TeamFile = {
  name: string;
  createdAt: number;
  members: TeamMember[];
};

type InboxMessage = {
  from: string;
  text: string;
  timestamp: string;
  read: boolean;
  summary?: string;
};

// Helpers

function encodeCwd(cwd: string): string {
  return cwd.replace(/\//g, "-");
}

function getLiveSessionIds(): Map<string, Set<string>> {
  const result = new Map<string, Set<string>>();
  if (!fs.existsSync(TMP_BASE)) return result;
  for (const dir of fs.readdirSync(TMP_BASE)) {
    if (!dir.startsWith("-")) continue;
    const fullPath = path.join(TMP_BASE, dir);
    if (!fs.statSync(fullPath).isDirectory()) continue;
    const sessions = new Set<string>();
    for (const sub of fs.readdirSync(fullPath)) {
      if (fs.statSync(path.join(fullPath, sub)).isDirectory()) {
        sessions.add(sub);
      }
    }
    if (sessions.size > 0) result.set(dir, sessions);
  }
  return result;
}

function getAllLiveIds(): Set<string> {
  const all = new Set<string>();
  for (const ids of getLiveSessionIds().values()) {
    for (const id of ids) all.add(id);
  }
  return all;
}

function getTeamFilePath(project: string): string {
  return path.join(TEAMS_DIR, project, "config.json");
}

function readTeamFile(project: string): TeamFile | null {
  const p = getTeamFilePath(project);
  try {
    return JSON.parse(fs.readFileSync(p, "utf-8"));
  } catch {
    return null;
  }
}

function writeTeamFile(project: string, data: TeamFile): void {
  const dir = path.join(TEAMS_DIR, project);
  fs.mkdirSync(dir, { recursive: true });
  const p = getTeamFilePath(project);
  // Atomic write: write to temp then rename
  const tmp = `${p}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(data));
  fs.renameSync(tmp, p);
}

function getInboxPath(sessionId: string): string {
  return path.join(MAILBOX_DIR, `${sessionId}.json`);
}

function readInbox(sessionId: string): InboxMessage[] {
  try {
    return JSON.parse(fs.readFileSync(getInboxPath(sessionId), "utf-8"));
  } catch {
    return [];
  }
}

function writeInbox(sessionId: string, messages: InboxMessage[]): void {
  fs.mkdirSync(MAILBOX_DIR, { recursive: true });
  const p = getInboxPath(sessionId);
  const tmp = `${p}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(messages));
  fs.renameSync(tmp, p);
}

function relativeTime(iso: string): string {
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

function findSessionByName(
  name: string
): { sessionId: string; project: string } | null {
  if (!fs.existsSync(TEAMS_DIR)) return null;
  for (const proj of fs.readdirSync(TEAMS_DIR)) {
    const team = readTeamFile(proj);
    if (!team) continue;
    const member = team.members.find((m) => m.name === name);
    if (member) return { sessionId: member.agentId, project: proj };
  }
  return null;
}

// MCP Server

const server = new McpServer({
  name: "cc",
  version: "0.2.0",
});

server.tool(
  "cc_peers",
  "Discover all live Claude Code sessions on this machine. Returns session IDs grouped by project, with metadata if available.",
  {},
  async () => {
    const live = getLiveSessionIds();
    const liveIds = getAllLiveIds();
    const lines: string[] = [];

    for (const [encoded, sessionIds] of live) {
      // Decode project name from encoded cwd (best effort — last segment)
      const parts = encoded.split("-").filter(Boolean);
      const project = parts[parts.length - 1] || encoded;
      const team = readTeamFile(project);

      lines.push(`${project} (${sessionIds.size} session${sessionIds.size > 1 ? "s" : ""}):`);

      for (const sid of sessionIds) {
        const member = team?.members.find((m) => m.agentId === sid);
        if (member) {
          const files =
            member.files.length > 0
              ? member.files.slice(-3).join(", ")
              : "no files yet";
          lines.push(
            `  ${member.name} (${member.branch || "no branch"}) — ${files} — "${member.task?.slice(0, 60) || ""}" — ${member.isActive ? "active" : "idle"}`
          );
        } else {
          lines.push(`  ${sid.slice(0, 8)}... (no metadata registered yet)`);
        }
      }
    }

    return {
      content: [
        {
          type: "text" as const,
          text: lines.length > 0 ? lines.join("\n") : "No live sessions detected.",
        },
      ],
    };
  }
);

server.tool(
  "cc_roster",
  "Show the roster for a specific project — all active sessions, what they're editing, and file conflicts. If project is omitted, uses the current working directory.",
  { project: z.string().optional().describe("Project name (defaults to basename of cwd)") },
  async ({ project }) => {
    const proj = project || path.basename(process.cwd());
    const team = readTeamFile(proj);
    const liveIds = getAllLiveIds();
    const lines: string[] = [];

    if (!team || team.members.length === 0) {
      // Check /tmp for unregistered sessions
      const encoded = encodeCwd(process.cwd());
      const live = getLiveSessionIds().get(encoded);
      if (live && live.size > 0) {
        lines.push(`[cc] ${live.size} session(s) detected on '${proj}' (no metadata yet)`);
      } else {
        return {
          content: [{ type: "text" as const, text: `No sessions found for project '${proj}'.` }],
        };
      }
    } else {
      // Filter to live members only
      const alive = team.members.filter((m) => liveIds.has(m.agentId));
      if (alive.length === 0) {
        return {
          content: [{ type: "text" as const, text: `No live sessions on '${proj}'.` }],
        };
      }

      lines.push(`[cc] ${alive.length} session(s) active on '${proj}'`);

      // Collect all files for conflict detection
      const fileOwners = new Map<string, string[]>();
      for (const m of alive) {
        for (const f of m.files) {
          if (!fileOwners.has(f)) fileOwners.set(f, []);
          fileOwners.get(f)!.push(m.name);
        }
      }

      for (const m of alive) {
        const branchTag = m.branch && m.branch !== "main" ? ` (${m.branch})` : "";
        const filesStr = m.files.length > 0 ? m.files.slice(-3).join(", ") : "no files yet";
        const taskStr = m.task ? ` — "${m.task.slice(0, 60)}"` : "";
        lines.push(`  -> ${m.name}${branchTag} editing: ${filesStr}${taskStr}`);
      }

      // File conflicts
      for (const [file, owners] of fileOwners) {
        if (owners.length > 1) {
          lines.push(`  !! ${owners.join(" + ")} are both touching ${file}`);
        }
      }
    }

    return {
      content: [{ type: "text" as const, text: lines.join("\n") }],
    };
  }
);

server.tool(
  "cc_send",
  "Send a message to another Claude Code session. The recipient sees it on their next prompt. Use cc_peers or cc_roster to find session names.",
  {
    to: z.string().describe("Recipient session name (e.g., 'vector-seo-2')"),
    text: z.string().describe("Message content"),
    summary: z
      .string()
      .optional()
      .describe("5-10 word preview (shown in roster)"),
  },
  async ({ to, text, summary }) => {
    // Find the target session
    const target = findSessionByName(to);
    if (!target) {
      return {
        content: [
          {
            type: "text" as const,
            text: `Session '${to}' not found. Use cc_peers to see available sessions.`,
          },
        ],
      };
    }

    // Check if target is alive
    if (!getAllLiveIds().has(target.sessionId)) {
      return {
        content: [
          { type: "text" as const, text: `Session '${to}' is no longer running.` },
        ],
      };
    }

    // Get our name from environment or team file
    const mySessionId = process.env.CLAUDE_SESSION_ID || "unknown";
    const myProject = path.basename(process.cwd());
    const myTeam = readTeamFile(myProject);
    const myName =
      myTeam?.members.find((m) => m.agentId === mySessionId)?.name || myProject;

    // Append to target's inbox
    const inbox = readInbox(target.sessionId);
    inbox.push({
      from: myName,
      text,
      timestamp: new Date().toISOString(),
      read: false,
      summary,
    });
    writeInbox(target.sessionId, inbox);

    return {
      content: [
        {
          type: "text" as const,
          text: `Message sent to ${to}. They'll see it on their next prompt.`,
        },
      ],
    };
  }
);

// Start
const transport = new StdioServerTransport();
await server.connect(transport);
