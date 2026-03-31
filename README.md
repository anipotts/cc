# cc

Multi-session awareness for Claude Code.

When you run multiple Claude Code sessions on the same machine, they don't know about each other. You end up manually relaying context between terminals. **cc** fixes that.

## What it does

Every time you send a prompt, cc tells your session who else is working:

```
[cc] 3 sessions active on 'vector-seo'
  -> vector-seo-2 (feat/api) editing: src/routes.ts, lib/seo.ts — "fixing crawl endpoint" — 12m ago
  -> vector-seo-3 editing: tests/api.test.ts — "writing integration tests" — 3m ago
  !! vector-seo-2 is also touching src/routes.ts
```

- See all active sessions on the same project
- File conflict warnings when two sessions touch the same file
- Send messages between sessions via `/cc`
- Dead sessions are detected instantly (no stale data)

## How it works

```
┌─────────────────────────────────────────────────────────┐
│  Claude Code Session A          Session B          ...  │
│       │                            │                    │
│  UserPromptSubmit             UserPromptSubmit          │
│       │                            │                    │
│       ▼                            ▼                    │
│  ┌─────────┐                  ┌─────────┐              │
│  │  cc.py   │◄── reads ──────►│  cc.py   │              │
│  │  roster  │                 │  roster  │              │
│  └────┬─────┘                 └────┬─────┘              │
│       │ writes                     │ writes             │
│       ▼                            ▼                    │
│  ~/.claude/cc/sessions/       ~/.claude/cc/sessions/    │
│    {session-a}.json              {session-b}.json       │
│                                                         │
│  Liveness: /tmp/claude-{uid}/{project}/{session-uuid}/  │
│  (managed by Claude Code — exists only while running)   │
└─────────────────────────────────────────────────────────┘
```

**Liveness detection** uses Claude Code's own `/tmp` directories. These directories exist only while a session is running — when it ends (or crashes), the directory disappears. No PIDs, no heartbeats, no TTLs. Instant and accurate.

**Metadata** (session name, files being edited, current task) is stored in `~/.claude/cc/sessions/` as one JSON file per session. Each session writes only its own file and reads everyone else's.

## Install

```bash
claude plugin add anipotts/cc
```

## Usage

cc works automatically once installed. Every prompt shows a roster if there are other sessions on the same project.

### `/cc` command

| Command | What it does |
|---------|-------------|
| `/cc` | Show all active sessions with details |
| `/cc <name>` | Pull in context from another session's transcript |
| `/cc <name> <message>` | Send a message to another session (delivered on their next prompt) |

## Plugin structure

```
cc/
├── .claude-plugin/plugin.json   Plugin manifest
├── hooks/
│   ├── cc.py                    Core logic (roster, touch, cleanup)
│   └── hooks.json               Hook registration
├── commands/cc.md               /cc command definition
└── evals/test_cc.py             Test suite (69 tests)
```

### Hooks

| Event | Handler | What it does |
|-------|---------|-------------|
| `UserPromptSubmit` | `roster` | Register session + show peer roster + file conflict warnings |
| `PostToolUse` (Edit/Write) | `touch` | Track which files this session is editing |
| `SessionEnd` | `cleanup` | Remove session metadata file |

## Requirements

- Claude Code v2.1+
- Python 3.10+
- macOS or Linux

## License

MIT
