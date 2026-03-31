#!/usr/bin/env python3
"""cc.py — multi-session awareness hooks for Claude Code.

Primary data source: ~/.claude/sessions/*.json (Claude Code's own registry).
Enrichment: ~/.claude/cc/enrich/{sessionId}.json (files, task — written by hooks).
Mailbox: ~/.claude/cc/mailbox/{sessionId}.json (cross-session messages).

Respects CLAUDE_CONFIG_DIR for portability across all environments.

Events:
  roster  — UserPromptSubmit: read sessions, write enrichment, output roster
  touch   — PostToolUse (Edit/Write): update files in enrichment
  cleanup — SessionEnd: remove enrichment file
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Resolve paths the same way Claude Code does (src/utils/envUtils.ts)
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
SESSIONS_DIR = CLAUDE_DIR / "sessions"
ENRICH_DIR = CLAUDE_DIR / "cc" / "enrich"
MAILBOX_DIR = CLAUDE_DIR / "cc" / "mailbox"
MAX_TRACKED_FILES = 20

PID_FILE_RE = re.compile(r"^\d+\.json$")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def context(msg: str) -> None:
    print(msg)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------

_ensured_dirs: set[str] = set()


def locked_write(path: Path, updater, default=None):
    """Atomic read-modify-write with advisory file locking."""
    parent = str(path.parent)
    if parent not in _ensured_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ensured_dirs.add(parent)

    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError, FileNotFoundError):
                data = default() if callable(default) else default
            result = updater(data)
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(result))
            tmp.rename(path)
            return result
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Session discovery (reads Claude Code's native registry)
# ---------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def read_live_sessions() -> list[dict]:
    """Read all live sessions from ~/.claude/sessions/*.json."""
    if not SESSIONS_DIR.is_dir():
        return []
    sessions = []
    for f in SESSIONS_DIR.iterdir():
        if not PID_FILE_RE.match(f.name):
            continue
        pid = int(f.stem)
        if not pid_alive(pid):
            continue
        try:
            data = json.loads(f.read_text())
            data["_pid"] = pid
            sessions.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return sessions


# ---------------------------------------------------------------------------
# Enrichment (our metadata layer on top of Claude Code's registry)
# ---------------------------------------------------------------------------

def enrich_path(session_id: str) -> Path:
    return ENRICH_DIR / f"{session_id}.json"


def read_enrichment(session_id: str) -> dict | None:
    try:
        return json.loads(enrich_path(session_id).read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------

def read_unread(session_id: str) -> list[dict]:
    try:
        return [m for m in json.loads((MAILBOX_DIR / f"{session_id}.json").read_text()) if not m.get("read")]
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return []


def mark_read(session_id: str) -> None:
    def updater(msgs):
        for m in msgs:
            m["read"] = True
        return msgs
    locked_write(MAILBOX_DIR / f"{session_id}.json", updater, default=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_session_id(payload: dict) -> str:
    return payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "")


def get_cwd(payload: dict) -> str:
    return payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_roster(payload: dict) -> None:
    """UserPromptSubmit: read sessions, write enrichment, output roster."""
    session_id = get_session_id(payload)
    if not session_id:
        log("[cc] no session_id, skipping")
        return

    cwd = get_cwd(payload)
    my_project = os.path.basename(cwd)

    # Read all live sessions from Claude Code's registry
    all_sessions = read_live_sessions()

    # Write/update own enrichment
    prompt = payload.get("user_prompt", "")
    existing = read_enrichment(session_id) or {}
    enrich = {
        "files": existing.get("files", []),
        "task": prompt[:120] if prompt else existing.get("task", ""),
        "updated": now_iso(),
    }
    ENRICH_DIR.mkdir(parents=True, exist_ok=True)
    ep = enrich_path(session_id)
    tmp = ep.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(enrich))
    tmp.rename(ep)

    # Build roster
    peers = [s for s in all_sessions if s.get("sessionId") != session_id]
    messages = read_unread(session_id)

    if not peers and not messages:
        return

    # Group by project
    my_files = set(enrich.get("files", []))
    same_project = []
    other_projects: dict[str, int] = {}

    for peer in peers:
        peer_cwd = peer.get("cwd", "")
        peer_proj = os.path.basename(peer_cwd)
        if peer_proj == my_project:
            same_project.append(peer)
        else:
            other_projects[peer_proj] = other_projects.get(peer_proj, 0) + 1

    # Output same-project peers
    if same_project:
        context(f"[cc] {len(same_project) + 1} sessions on '{my_project}'")
        for peer in same_project:
            name = peer.get("name") or peer.get("sessionId", "?")[:8]
            peer_enrich = read_enrichment(peer.get("sessionId", "")) or {}
            files = peer_enrich.get("files", [])
            task = peer_enrich.get("task", "")
            files_str = ", ".join(files[-3:]) if files else ""
            task_str = f' — "{task[:50]}"' if task else ""
            line = f"  └ {name}"
            if files_str:
                line += f"  {files_str}"
            line += task_str
            context(line)

            # File conflicts
            for cf in my_files & set(files):
                context(f"  !! {name} is also touching {cf}")

    # Cross-project summary
    if other_projects:
        parts = [f"{p}({c})" for p, c in sorted(other_projects.items(), key=lambda x: -x[1])[:5]]
        context(f"[cc] also: {', '.join(parts)}")

    # Messages
    if messages:
        context(f"[cc] {len(messages)} message(s):")
        for msg in messages:
            context(f"  └ {msg.get('from', '?')}: {msg.get('text', msg.get('content', ''))[:200]}")
        mark_read(session_id)


def handle_touch(payload: dict) -> None:
    """PostToolUse (Edit/Write): update files in enrichment."""
    session_id = get_session_id(payload)
    if not session_id:
        return

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    cwd = get_cwd(payload)
    if file_path.startswith(cwd):
        file_path = file_path[len(cwd):].lstrip("/")

    def updater(data):
        if not data:
            data = {"files": [], "task": "", "updated": now_iso()}
        files = data.get("files", [])
        if file_path not in files:
            files.append(file_path)
            if len(files) > MAX_TRACKED_FILES:
                files = files[-MAX_TRACKED_FILES:]
            data["files"] = files
            data["updated"] = now_iso()
        return data

    locked_write(enrich_path(session_id), updater, default=dict)


def handle_cleanup(payload: dict) -> None:
    """SessionEnd: remove enrichment file."""
    session_id = get_session_id(payload)
    if not session_id:
        return
    try:
        enrich_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass
    log(f"[cc] cleaned up {session_id[:8]}")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

HANDLERS = {
    "roster": handle_roster,
    "touch": handle_touch,
    "cleanup": handle_cleanup,
}


def main() -> None:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <roster|touch|cleanup>", file=sys.stderr)
        sys.exit(1)

    event = sys.argv[1]
    if event not in HANDLERS:
        print(f"[cc] unknown event: {event}", file=sys.stderr)
        sys.exit(1)

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        payload = {}

    try:
        HANDLERS[event](payload)
    except Exception as e:
        log(f"[cc] {event} error: {e}")
        sys.exit(0)


if __name__ == "__main__":
    main()
