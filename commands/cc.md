---
description: See active Claude Code sessions and communicate across them
argument-hint: [session-name] [message]
allowed-tools:
  - Bash
  - Read
---

# /cc — Multi-Session Awareness

You are the cc plugin's command handler. The user typed `/cc` with these arguments: $ARGUMENTS

## What to do

### No arguments (`/cc`)

Show a rich roster of all active Claude Code sessions. Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/hooks/cc.py roster <<< '{}'
```

Then also read the session files directly for a richer view:

```bash
ls -1 ~/.claude/cc/sessions/*.json 2>/dev/null
```

For each session file, read it and display a formatted table:

| Session | Project | Branch | Files | Last Active | Task |
|---------|---------|--------|-------|-------------|------|

If no sessions exist, say "No other cc sessions detected. This session will register on your next prompt."

### With a session name (`/cc <name>`)

The user wants context from another session. Find the session file matching that name:

```bash
grep -l '"name": "<name>"' ~/.claude/cc/sessions/*.json 2>/dev/null
```

Read that session's state file and display it. Then find and read the last 30 messages from that session's JSONL transcript. Transcripts are at:

```
~/.claude/projects/{project-dir-path}/sessions/{session-id}/transcript.jsonl
```

The project dir path uses dashes for slashes (e.g., `-Users-anipotts-Code-active-cc`). Read the transcript's last 30 lines and summarize what that session has been doing.

### With a session name and message (`/cc <name> <message>`)

The user wants to send a message to another session's mailbox. Find the target session's ID from its state file, then write a message:

```bash
mkdir -p ~/.claude/cc/mailbox/{target-session-id}
echo '{"from": "this-session-name", "content": "<message>", "timestamp": "now"}' > ~/.claude/cc/mailbox/{target-session-id}/$(date +%s%N).json
```

Confirm the message was sent: "Message sent to {name}. They'll see it on their next prompt."

## Important

- Never fabricate session data. Only show what's in the files.
- Session files are at `~/.claude/cc/sessions/*.json`
- Liveness is determined by `/tmp/claude-{uid}/` directories, not PIDs. If a session's `/tmp` dir is gone, it's dead — clean up its metadata file.
- Keep output concise and scannable.
