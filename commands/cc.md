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

You are the cc plugin's command handler. The user typed `/cc` with these arguments: $ARGUMENTS

## What to do

### No arguments (`/cc`)

Call the `cc_roster` MCP tool to show a detailed roster of all active sessions.

If the MCP tool is not available, fall back to reading team files directly:

```bash
ls ~/.claude/cc/teams/*/config.json 2>/dev/null
```

For each team file, read it and display a formatted roster showing all members.

### With a session name (`/cc <name>`)

The user wants context from another session. First call `cc_roster` to verify the session exists, then find and read the last 30 messages from that session's JSONL transcript at:

```
~/.claude/projects/{project-dir-path}/sessions/{session-id}/transcript.jsonl
```

Summarize what that session has been doing.

### With a session name and message (`/cc <name> <message>`)

Call the `cc_send` MCP tool:
- `to`: the session name
- `text`: the message
- `summary`: a 5-10 word summary of the message

Confirm: "Message sent to {name}. They'll see it on their next prompt."

## Important

- Prefer MCP tools (`cc_roster`, `cc_peers`, `cc_send`) over bash commands.
- Liveness is determined by `/tmp/claude-{uid}/` directories. If a session's `/tmp` dir is gone, it's dead.
- Keep output concise and scannable.
