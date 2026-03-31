#!/usr/bin/env npx tsx
/**
 * cc MCP server — native tools for multi-session awareness.
 *
 * Gives Claude cc_peers, cc_roster, and cc_send as first-class tools.
 * Uses /tmp/claude-{uid}/ for liveness, ~/.claude/cc/ for metadata and mailbox.
 * All writes use atomic rename. Reads from the hook's locked team files.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

const CC_DIR = path.join(os.homedir(), ".claude", "cc");
const TEAMS_DIR = path.join(CC_DIR, "teams");
const MAILBOX_DIR = path.join(CC_DIR, "mailbox");
const TMP_BASE = path.join("/tmp", `claude-${process.getuid!()}`);

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

// --- Helpers ---

function getLiveSessionIds(): Map<string, Set<string>> {
  const result = new Map<string, Set<string>>();
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(TMP_BASE, { withFileTypes: true });
  } catch {
    return result;
  }
  for (const dir of entries) {
    if (!dir.isDirectory() || !dir.name.startsWith("-")) continue;
    const sessions = new Set<string>();
    try {
      for (const sub of fs.readdirSync(path.join(TMP_BASE, dir.name), { withFileTypes: true })) {
        if (sub.isDirectory()) sessions.add(sub.name);
      }
    } catch { continue; }
    if (sessions.size > 0) result.set(dir.name, sessions);
  }
  return result;
}

function flattenLiveIds(live: Map<string, Set<string>>): Set<string> {
  const all = new Set<string>();
  for (const ids of live.values()) for (const id of ids) all.add(id);
  return all;
}

function readTeamFile(project: string): TeamFile | null {
  try {
    return JSON.parse(fs.readFileSync(path.join(TEAMS_DIR, project, "config.json"), "utf-8"));
  } catch {
    return null;
  }
}

function readInbox(sessionId: string): InboxMessage[] {
  try {
    return JSON.parse(fs.readFileSync(path.join(MAILBOX_DIR, `${sessionId}.json`), "utf-8"));
  } catch {
    return [];
  }
}

function writeInbox(sessionId: string, messages: InboxMessage[]): void {
  fs.mkdirSync(MAILBOX_DIR, { recursive: true });
  const p = path.join(MAILBOX_DIR, `${sessionId}.json`);
  const tmp = `${p}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(messages));
  fs.renameSync(tmp, p);
}

function findSessionByName(name: string): { sessionId: string; project: string } | null {
  let teams: string[];
  try {
    teams = fs.readdirSync(TEAMS_DIR);
  } catch {
    return null;
  }
  for (const proj of teams) {
    const team = readTeamFile(proj);
    if (!team) continue;
    const member = team.members.find((m) => m.name === name);
    if (member) return { sessionId: member.agentId, project: proj };
  }
  return null;
}

// --- MCP Server ---

const server = new McpServer({ name: "cc", version: "0.2.0" });

server.tool(
  "cc_peers",
  "Discover all live Claude Code sessions on this machine, grouped by project.",
  {},
  async () => {
    const live = getLiveSessionIds();
    const lines: string[] = [];

    for (const [encoded, sessionIds] of live) {
      const parts = encoded.split("-").filter(Boolean);
      const project = parts[parts.length - 1] || encoded;
      const team = readTeamFile(project);

      lines.push(`${project} (${sessionIds.size} session${sessionIds.size > 1 ? "s" : ""}):`);

      for (const sid of sessionIds) {
        const member = team?.members.find((m) => m.agentId === sid);
        if (member) {
          const files = member.files.length > 0 ? member.files.slice(-3).join(", ") : "no files yet";
          lines.push(`  -> ${member.name} (${member.branch || "no branch"}) editing: ${files} — "${member.task?.slice(0, 60) || ""}"`);
        } else {
          lines.push(`  -> ${sid.slice(0, 8)}... (no metadata yet)`);
        }
      }
    }

    return {
      content: [{ type: "text" as const, text: lines.length > 0 ? lines.join("\n") : "No live sessions detected." }],
    };
  }
);

server.tool(
  "cc_roster",
  "Show the roster for a specific project — active sessions, files being edited, and file conflicts.",
  { project: z.string().optional().describe("Project name (defaults to basename of cwd)") },
  async ({ project }) => {
    const proj = project || path.basename(process.cwd());
    const team = readTeamFile(proj);
    const live = getLiveSessionIds();
    const liveIds = flattenLiveIds(live);
    const lines: string[] = [];

    if (!team || team.members.length === 0) {
      const encoded = proj.replace(/\//g, "-");
      const liveSessions = live.get(`-${encoded}`) || live.get(encoded);
      if (liveSessions && liveSessions.size > 0) {
        lines.push(`[cc] ${liveSessions.size} session(s) on '${proj}' (no metadata yet)`);
      } else {
        return { content: [{ type: "text" as const, text: `No sessions found for '${proj}'.` }] };
      }
    } else {
      const alive = team.members.filter((m) => liveIds.has(m.agentId));
      if (alive.length === 0) {
        return { content: [{ type: "text" as const, text: `No live sessions on '${proj}'.` }] };
      }

      lines.push(`[cc] ${alive.length} session(s) active on '${proj}'`);

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

      for (const [file, owners] of fileOwners) {
        if (owners.length > 1) {
          lines.push(`  !! ${owners.join(" + ")} are both touching ${file}`);
        }
      }
    }

    return { content: [{ type: "text" as const, text: lines.join("\n") }] };
  }
);

server.tool(
  "cc_send",
  "Send a message to another Claude Code session. They see it on their next prompt. Use cc_peers to find session names.",
  {
    to: z.string().describe("Recipient session name (e.g., 'vector-seo-2')"),
    text: z.string().describe("Message content"),
    summary: z.string().optional().describe("5-10 word preview"),
  },
  async ({ to, text, summary }) => {
    const target = findSessionByName(to);
    if (!target) {
      return { content: [{ type: "text" as const, text: `Session '${to}' not found. Use cc_peers to see available sessions.` }] };
    }

    const live = getLiveSessionIds();
    if (!flattenLiveIds(live).has(target.sessionId)) {
      return { content: [{ type: "text" as const, text: `Session '${to}' is no longer running.` }] };
    }

    const mySessionId = process.env.CLAUDE_SESSION_ID || "unknown";
    const myProject = path.basename(process.cwd());
    const myTeam = readTeamFile(myProject);
    const myName = myTeam?.members.find((m) => m.agentId === mySessionId)?.name || myProject;

    const inbox = readInbox(target.sessionId);
    inbox.push({ from: myName, text, timestamp: new Date().toISOString(), read: false, summary });
    writeInbox(target.sessionId, inbox);

    return { content: [{ type: "text" as const, text: `Sent to ${to}. They'll see it next prompt.` }] };
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);
