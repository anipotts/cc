#!/usr/bin/env npx tsx
/**
 * cc MCP server — reads Claude Code's native session registry.
 *
 * Primary: ~/.claude/sessions/*.json (Claude Code's own concurrentSessions)
 * Enrichment: ~/.claude/cc/enrich/{sessionId}.json (files, task from hooks)
 * Mailbox: ~/.claude/cc/mailbox/{sessionId}.json
 *
 * Respects CLAUDE_CONFIG_DIR for portability.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";
import { execSync } from "child_process";

const CLAUDE_DIR = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
const SESSIONS_DIR = path.join(CLAUDE_DIR, "sessions");
const ENRICH_DIR = path.join(CLAUDE_DIR, "cc", "enrich");
const MAILBOX_DIR = path.join(CLAUDE_DIR, "cc", "mailbox");

type Session = {
  pid: number;
  sessionId: string;
  cwd: string;
  name?: string;
  kind?: string;
  startedAt?: number;
  busy: boolean;
  files: string[];
  task: string;
};

function isProcessAlive(pid: number): boolean {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

function getCpuPercent(pid: number): number {
  try {
    const out = execSync(`ps -p ${pid} -o %cpu=`, { encoding: "utf-8", timeout: 1000 });
    return parseFloat(out.trim()) || 0;
  } catch { return 0; }
}

function readLiveSessions(): Session[] {
  let files: string[];
  try { files = fs.readdirSync(SESSIONS_DIR); } catch { return []; }

  const sessions: Session[] = [];
  for (const f of files) {
    if (!/^\d+\.json$/.test(f)) continue;
    const pid = parseInt(f.slice(0, -5), 10);
    if (!isProcessAlive(pid)) continue;

    try {
      const data = JSON.parse(fs.readFileSync(path.join(SESSIONS_DIR, f), "utf-8"));
      const sid = data.sessionId || "";
      let enrichFiles: string[] = [];
      let enrichTask = "";
      try {
        const e = JSON.parse(fs.readFileSync(path.join(ENRICH_DIR, `${sid}.json`), "utf-8"));
        enrichFiles = e.files || [];
        enrichTask = e.task || "";
      } catch {}

      sessions.push({
        pid,
        sessionId: sid,
        cwd: data.cwd || "",
        name: data.name,
        kind: data.kind || "interactive",
        startedAt: data.startedAt,
        busy: getCpuPercent(pid) > 5,
        files: enrichFiles,
        task: enrichTask,
      });
    } catch { continue; }
  }
  return sessions;
}

function findSession(query: string): Session | undefined {
  const sessions = readLiveSessions();
  return sessions.find(s => s.name === query)
    || sessions.find(s => s.name?.toLowerCase() === query.toLowerCase())
    || sessions.find(s => s.sessionId.startsWith(query))
    || sessions.find(s => String(s.pid) === query);
}

// --- MCP Server ---

const server = new McpServer({ name: "cc", version: "0.5.0" });

server.tool(
  "cc_peers",
  "Discover all live Claude Code sessions on this machine with busy/idle status.",
  {},
  async () => {
    const sessions = readLiveSessions();
    if (sessions.length === 0) return { content: [{ type: "text" as const, text: "No live sessions." }] };

    const busy = sessions.filter(s => s.busy);
    const idle = sessions.filter(s => !s.busy);

    const lines = [`cc — ${sessions.length} sessions (${busy.length} busy, ${idle.length} idle)`, ""];

    // Group by project
    const byProj = new Map<string, Session[]>();
    for (const s of sessions) {
      const proj = path.basename(s.cwd);
      if (!byProj.has(proj)) byProj.set(proj, []);
      byProj.get(proj)!.push(s);
    }

    for (const [proj, members] of byProj) {
      lines.push(`  ${proj} (${members.length})`);
      for (let i = 0; i < members.length; i++) {
        const m = members[i]!;
        const conn = i === members.length - 1 ? "└" : "├";
        const status = m.busy ? "▶" : "·";
        const name = m.name || m.kind || m.sessionId.slice(0, 8);
        const filesStr = m.files.length > 0 ? `  ${m.files.slice(-3).join(", ")}` : "";
        const taskStr = m.task ? `  "${m.task.slice(0, 50)}"` : "";
        lines.push(`  ${conn} ${status} ${name}${filesStr}${taskStr}`);
      }
      lines.push("");
    }

    return { content: [{ type: "text" as const, text: lines.join("\n") }] };
  }
);

server.tool(
  "cc_roster",
  "Show sessions for a specific project with file conflict detection.",
  { project: z.string().optional().describe("Project name (defaults to cwd basename)") },
  async ({ project }) => {
    const proj = project || path.basename(process.cwd());
    const sessions = readLiveSessions();
    const matching = sessions.filter(s => path.basename(s.cwd) === proj);

    if (matching.length === 0) {
      return { content: [{ type: "text" as const, text: `No sessions on '${proj}'.` }] };
    }

    const lines = [`[cc] ${matching.length} session(s) on '${proj}'`];

    const fileOwners = new Map<string, string[]>();
    for (const m of matching) {
      for (const f of m.files) {
        if (!fileOwners.has(f)) fileOwners.set(f, []);
        fileOwners.get(f)!.push(m.name || m.sessionId.slice(0, 8));
      }
    }

    for (const m of matching) {
      const status = m.busy ? "▶" : "·";
      const name = m.name || m.sessionId.slice(0, 8);
      const filesStr = m.files.length > 0 ? m.files.slice(-3).join(", ") : "no files yet";
      const taskStr = m.task ? ` — "${m.task.slice(0, 50)}"` : "";
      lines.push(`  └ ${status} ${name}  ${filesStr}${taskStr}`);
    }

    for (const [file, owners] of fileOwners) {
      if (owners.length > 1) lines.push(`  !! ${owners.join(" + ")} both touching ${file}`);
    }

    return { content: [{ type: "text" as const, text: lines.join("\n") }] };
  }
);

server.tool(
  "cc_send",
  "Send a message to another Claude Code session by name. They see it on their next prompt.",
  {
    to: z.string().describe("Recipient session name"),
    text: z.string().describe("Message content"),
    summary: z.string().optional().describe("5-10 word preview"),
  },
  async ({ to, text, summary }) => {
    const target = findSession(to);
    if (!target) {
      return { content: [{ type: "text" as const, text: `Session '${to}' not found. Use cc_peers to see available sessions.` }] };
    }

    fs.mkdirSync(MAILBOX_DIR, { recursive: true });
    const inboxPath = path.join(MAILBOX_DIR, `${target.sessionId}.json`);
    let inbox: any[] = [];
    try { inbox = JSON.parse(fs.readFileSync(inboxPath, "utf-8")); } catch {}

    const myId = process.env.CLAUDE_SESSION_ID || "unknown";
    const sessions = readLiveSessions();
    const me = sessions.find(s => s.sessionId === myId);
    const myName = me?.name || path.basename(process.cwd());

    inbox.push({ from: myName, text, timestamp: new Date().toISOString(), read: false, summary });
    const tmp = `${inboxPath}.tmp.${process.pid}`;
    fs.writeFileSync(tmp, JSON.stringify(inbox));
    fs.renameSync(tmp, inboxPath);

    return { content: [{ type: "text" as const, text: `Sent to ${to}.` }] };
  }
);

const transport = new StdioServerTransport();
await server.connect(transport);
