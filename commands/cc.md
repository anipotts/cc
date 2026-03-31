---
description: See active Claude Code sessions and communicate across them
argument-hint: [session-name] [message]
allowed-tools:
  - Bash
  - Read
  - mcp__cc__cc_roster
  - mcp__cc__cc_peers
  - mcp__cc__cc_send
---

# /cc — Multi-Session Awareness

The user typed `/cc` with: $ARGUMENTS

## Rules for output

- Output must be **clean and human-readable** — no XML, no raw JSON, no code blocks for data
- Use simple text with arrows (→), bullets (•), and indentation
- Keep it short and scannable
- Show relative times ("5m ago"), not timestamps

## No arguments — show roster

Call the `cc_peers` MCP tool. Format the result as a clean roster:

```
cc — 3 sessions on 'vector-seo'

  → vector-seo       main        editing: src/routes.ts, lib/seo.ts     5m ago
                                  "fixing crawl endpoint"
  → vector-seo-2     feat/api    editing: tests/api.test.ts             12m ago
                                  "writing integration tests"

  ⚠ Both touching src/routes.ts
```

If the MCP tool is unavailable, scan `/tmp/claude-{uid}/` and read `~/.claude/cc/teams/*/config.json` directly.

## With session name — pull context

`/cc <name>`: Find that session's ID from team files, then read the last 20 lines of their transcript at `~/.claude/projects/{path}/sessions/{id}/transcript.jsonl`. Summarize what they've been doing in 3-5 bullets.

## With session name + message — send message

`/cc <name> <message>`: Call `cc_send` MCP tool with:
- `to`: the session name
- `text`: the message
- `summary`: auto-generate a 5-word summary

Confirm: "Sent to {name} — they'll see it next prompt."
