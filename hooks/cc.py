#!/usr/bin/env python3
"""cc.py — multi-session awareness hooks for Claude Code.

Liveness: /tmp/claude-{uid}/ directories (managed by Claude Code).
Metadata: ~/.claude/cc/teams/{project}/config.json (one team file per project).
Mailbox: ~/.claude/cc/mailbox/{session-id}.json (per-session inbox).

All file writes use fcntl.flock() for concurrent safety, matching
Anthropic's proper-lockfile pattern from src/utils/teammateMailbox.ts.

Events:
  roster  — UserPromptSubmit: register self, read peers, output XML roster
  touch   — PostToolUse (Edit/Write): update files list in team file
  cleanup — SessionEnd: remove self from team file

stdout = visible to Claude as context
stderr = debug logging only
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CC_DIR = Path.home() / ".claude" / "cc"
TEAMS_DIR = CC_DIR / "teams"
MAILBOX_DIR = CC_DIR / "mailbox"
TMP_BASE = Path(f"/tmp/claude-{os.getuid()}")
MAX_TRACKED_FILES = 20


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def context(msg: str) -> None:
    print(msg)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode_cwd(cwd: str) -> str:
    return cwd.replace("/", "-")


# ---------------------------------------------------------------------------
# File locking (matches Anthropic's proper-lockfile pattern)
# ---------------------------------------------------------------------------

def locked_read_modify_write(path: Path, updater, default=None):
    """Atomic read-modify-write with file locking.

    The updater function receives the current data and returns the new data.
    Uses fcntl.flock for POSIX advisory locking.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")

    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    data = default() if callable(default) else default
            else:
                data = default() if callable(default) else default

            result = updater(data)

            # Atomic write: temp file then rename
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(result))
            tmp.rename(path)

            return result
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# /tmp liveness detection
# ---------------------------------------------------------------------------

def get_all_live_sessions() -> dict[str, set[str]]:
    """Scan /tmp for all live Claude Code sessions."""
    if not TMP_BASE.is_dir():
        return {}
    result = {}
    for d in TMP_BASE.iterdir():
        if not d.is_dir() or not d.name.startswith("-"):
            continue
        session_ids = {s.name for s in d.iterdir() if s.is_dir()}
        if session_ids:
            result[d.name] = session_ids
    return result


def get_all_live_ids() -> set[str]:
    ids: set[str] = set()
    for s in get_all_live_sessions().values():
        ids.update(s)
    return ids


# ---------------------------------------------------------------------------
# Team file operations
# ---------------------------------------------------------------------------

def team_file_path(project: str) -> Path:
    return TEAMS_DIR / project / "config.json"


def read_team_file(project: str) -> dict | None:
    try:
        return json.loads(team_file_path(project).read_text())
    except (json.JSONDecodeError, OSError):
        return None


def empty_team(project: str) -> dict:
    return {"name": project, "createdAt": int(time.time()), "members": []}


# ---------------------------------------------------------------------------
# Mailbox operations (matches Anthropic's TeammateMessage schema)
# ---------------------------------------------------------------------------

def read_inbox(session_id: str) -> list[dict]:
    p = MAILBOX_DIR / f"{session_id}.json"
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def read_unread_messages(session_id: str) -> list[dict]:
    return [m for m in read_inbox(session_id) if not m.get("read")]


def mark_messages_read(session_id: str) -> None:
    """Mark all messages as read (not deleted — matches Anthropic's pattern)."""
    p = MAILBOX_DIR / f"{session_id}.json"
    if not p.exists():
        return

    def updater(messages):
        for m in messages:
            m["read"] = True
        return messages

    locked_read_modify_write(p, updater, default=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_session_id(payload: dict) -> str:
    """Get session ID from hook payload, env var, or /tmp scan (last resort)."""
    sid = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "")
    if sid:
        return sid
    # Last resort: if we're in a project dir, find our session from /tmp
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    encoded = encode_cwd(cwd)
    tmp_dir = TMP_BASE / encoded
    if tmp_dir.is_dir():
        sessions = [s.name for s in tmp_dir.iterdir() if s.is_dir()]
        if len(sessions) == 1:
            return sessions[0]
    return ""


def get_cwd(payload: dict) -> str:
    return payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def get_branch(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def get_project(cwd: str) -> str:
    return os.path.basename(cwd)


def sanitize_id(session_id: str) -> str:
    return session_id.replace("/", "").replace("\\", "").replace("..", "").replace("\x00", "") or "unknown"


def make_name(project: str, branch: str, session_id: str, members: list[dict]) -> str:
    base = f"{project}-{branch}" if branch and branch != "main" else project
    taken = {m.get("name", "") for m in members if m.get("agentId") != session_id}
    if base not in taken:
        return base
    for i in range(2, 100):
        candidate = f"{base}-{i}"
        if candidate not in taken:
            return candidate
    return f"{base}-{session_id[:6]}"


def relative_time(iso_str: str) -> str:
    try:
        clean = iso_str.split(".")[0].replace("Z", "")
        dt = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - dt).total_seconds()
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_roster(payload: dict) -> None:
    """UserPromptSubmit: register self in team file, output XML roster."""
    session_id = get_session_id(payload)
    if not session_id:
        log("[cc] no session_id, skipping")
        return

    cwd = get_cwd(payload)
    project = get_project(cwd)
    live_ids = get_all_live_ids()
    ts = now_iso()

    # Update team file: register/update self, prune dead members
    def updater(team):
        if team is None:
            team = empty_team(project)

        # Prune dead members
        team["members"] = [m for m in team["members"] if m.get("agentId") in live_ids]

        # Find or create self
        self_member = next((m for m in team["members"] if m["agentId"] == session_id), None)

        prompt = payload.get("user_prompt", "")
        task = prompt[:120] if prompt else (self_member or {}).get("task", "")

        if self_member:
            # Update existing
            self_member["task"] = task
            self_member["isActive"] = True
            # Cache branch: only resolve on first registration
        else:
            branch = get_branch(cwd)
            name = make_name(project, branch, session_id, team["members"])
            team["members"].append({
                "agentId": session_id,
                "name": name,
                "cwd": cwd,
                "branch": branch,
                "files": [],
                "task": task,
                "isActive": True,
                "joinedAt": int(time.time()),
            })

        return team

    team = locked_read_modify_write(team_file_path(project), updater, default=lambda: empty_team(project))

    # Get peers (everyone except self)
    peers = [m for m in team["members"] if m["agentId"] != session_id]

    # Check for unregistered live sessions on same project
    encoded = encode_cwd(cwd)
    all_live = get_all_live_sessions()
    live_here = all_live.get(encoded, set())
    known_ids = {m["agentId"] for m in team["members"]}
    unregistered = live_here - known_ids
    for _ in unregistered:
        peers.append({
            "name": f"{project}-?",
            "branch": "",
            "files": [],
            "task": "(just started — no metadata yet)",
            "isActive": True,
        })

    # Read unread messages
    messages = read_unread_messages(session_id)

    if not peers and not messages:
        return

    self_member = next((m for m in team["members"] if m["agentId"] == session_id), None)
    my_files = set((self_member or {}).get("files", []))
    my_branch = (self_member or {}).get("branch", "")

    if peers:
        context(f"[cc] {len(peers) + 1} sessions on '{project}'")
        for peer in peers:
            peer_name = peer.get("name", "?")
            peer_branch = peer.get("branch", "")
            peer_files = peer.get("files", [])
            peer_task = peer.get("task", "")

            branch_tag = f" ({peer_branch})" if peer_branch and peer_branch != my_branch else ""
            files_str = ", ".join(peer_files[-3:]) if peer_files else "no files yet"
            task_str = f' — "{peer_task[:60]}"' if peer_task else ""

            context(f"  -> {peer_name}{branch_tag} editing: {files_str}{task_str}")

            conflicts = my_files & set(peer_files)
            for cf in conflicts:
                context(f"  !! {peer_name} is also touching {cf}")

    if messages:
        context(f"[cc] {len(messages)} message(s):")
        for msg in messages:
            from_name = msg.get("from", "?")
            text = msg.get("text", msg.get("content", ""))[:200]
            context(f"  <- {from_name}: {text}")

        # Mark as read (not deleted)
        mark_messages_read(session_id)


def handle_touch(payload: dict) -> None:
    """PostToolUse (Edit/Write): update files list in team file."""
    session_id = get_session_id(payload)
    if not session_id:
        return

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    cwd = get_cwd(payload)
    project = get_project(cwd)
    tf = team_file_path(project)
    if not tf.exists():
        return

    # Make path relative
    if file_path.startswith(cwd):
        file_path = file_path[len(cwd):].lstrip("/")

    def updater(team):
        if not team:
            return team
        for member in team.get("members", []):
            if member.get("agentId") == session_id:
                files = member.get("files", [])
                if file_path not in files:
                    files.append(file_path)
                    if len(files) > MAX_TRACKED_FILES:
                        files = files[-MAX_TRACKED_FILES:]
                    member["files"] = files
                break
        return team

    locked_read_modify_write(tf, updater)


def handle_cleanup(payload: dict) -> None:
    """SessionEnd: remove self from team file."""
    session_id = get_session_id(payload)
    if not session_id:
        return

    # Find which project we're in
    cwd = get_cwd(payload)
    project = get_project(cwd)
    tf = team_file_path(project)
    if not tf.exists():
        return

    def updater(team):
        if not team:
            return team
        team["members"] = [m for m in team.get("members", []) if m.get("agentId") != session_id]
        return team

    locked_read_modify_write(tf, updater)
    log(f"[cc] cleaned up session {session_id[:8]}")


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
        sys.exit(0)  # never block Claude Code


if __name__ == "__main__":
    main()
