# Privacy Policy

**cc** — Multi-session awareness for Claude Code

Last updated: March 31, 2026

## Data Collection

cc does **not** collect, transmit, or store any personal data. All data stays on your local machine.

## What cc accesses

| Data | Where it's stored | Purpose |
|------|------------------|---------|
| Session metadata (name, branch, files being edited) | `~/.claude/cc/teams/` | Show which sessions are active |
| Messages between sessions | `~/.claude/cc/mailbox/` | Cross-session communication |
| `/tmp/claude-{uid}/` directories | System temp | Detect which sessions are alive |

## What cc does NOT do

- Does not send data to any external server
- Does not access the internet
- Does not read your code or conversation content
- Does not collect analytics or telemetry
- Does not access any API keys or credentials
- Does not modify any files outside `~/.claude/cc/`

## Data lifecycle

- Session metadata is created when you send a prompt and deleted when the session ends
- Messages are marked as read after delivery, not permanently stored
- All data is ephemeral and cleaned up automatically via `/tmp` lifecycle and session cleanup hooks

## Third-party dependencies

cc uses the `@modelcontextprotocol/sdk` package (by Anthropic) for MCP server functionality. No other third-party services are contacted.

## Contact

For questions about this privacy policy: hello@anipotts.com
