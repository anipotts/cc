---
description: See active Claude Code sessions and communicate across them
argument-hint: [session-name] [message]
model: haiku
allowed-tools:
  - Bash
  - mcp__cc__cc_send
---

# /cc

Args: $ARGUMENTS

No arguments? Run this and display the output:
```
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/cc.py roster-cli "$(pwd)"
```

With `<name> <message>`? Call `cc_send` tool with `to` = first word, `text` = rest.

With just `<name>`? Run the roster command and highlight that session.
