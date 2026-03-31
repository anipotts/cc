---
description: See active Claude Code sessions and communicate across them
argument-hint: [session-name] [message]
model: haiku
allowed-tools:
  - Bash
  - Read
  - mcp__cc__cc_roster
  - mcp__cc__cc_peers
  - mcp__cc__cc_send
---

# /cc — Multi-Session Awareness

The user typed `/cc` with: $ARGUMENTS

## Rules

- **Always scan `/tmp/claude-{uid}/` as the source of truth** — not team files, not session files
- Output must be clean text — no XML, no code blocks for data
- Show relative times, truncated paths, short names

## No arguments — show all sessions

Run this bash command to get the ground truth:

```bash
uid=$(id -u); for d in /tmp/claude-${uid}/-*/; do [ -d "$d" ] || continue; n=$(basename "$d"); p=$(echo "$n" | rev | cut -d- -f1 | rev); c=0; for s in "$d"*/; do [ -d "$s" ] && c=$((c+1)); done; [ $c -gt 0 ] && echo "$p: $c session(s)"; done
```

Then read any team files for enrichment: `ls ~/.claude/cc/teams/*/config.json 2>/dev/null`

Format output like:

```
cc — 12 sessions across 5 projects

  fullstack (2)     ← you are here
    → fullstack-ani-dev  ani-dev  editing: src/app.ts  — "fix auth"
    → fullstack-2        ani-dev  (no metadata yet)

  vector-seo (4)
    → vector-seo         main     editing: src/routes.ts

  Content (3)
  spring (1)
  cc (1)
```

Current project goes first, others sorted by session count descending. Sessions with team file metadata show name + branch + files + task. Sessions without metadata show "(no metadata yet)".

## With session name — pull context

`/cc <name>`: Find that session's ID from team files, read last 20 lines of their transcript.

## With session name + message

`/cc <name> <message>`: Call `cc_send` MCP tool. Confirm delivery.

## Important

- The ground truth for "which sessions are alive" is ALWAYS `/tmp/claude-{uid}/`
- Team files at `~/.claude/cc/teams/*/config.json` are just metadata enrichment
- Never report "just this session" if /tmp shows more sessions — they're real, just unregistered
