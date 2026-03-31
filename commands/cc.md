---
description: See active Claude Code sessions and communicate across them
argument-hint: [session-name] [message]
model: haiku
allowed-tools:
  - Bash
  - mcp__cc__cc_send
---

# /cc

Arguments: $ARGUMENTS

If no arguments, run this single command and display its output exactly as-is:

```
bash ${CLAUDE_PLUGIN_ROOT}/scripts/roster.sh "$(pwd)"
```

If arguments contain a session name and message (e.g., `/cc researcher check the tests`), call the `cc_send` MCP tool with `to` as the first word and `text` as the rest.

If arguments contain just a session name (e.g., `/cc researcher`), run the roster script and highlight that session's info.
