#!/usr/bin/env python3
"""cc.py — multi-session awareness hooks for Claude Code.

Liveness: /tmp/claude-{uid}/ directories (managed by Claude Code).
Metadata: ~/.claude/cc/teams/{project}/config.json (one team file per project).
Mailbox: ~/.claude/cc/mailbox/{session-id}.json (per-session inbox).

All file writes use fcntl.flock() for concurrent safety, matching
Anthropic's proper-lockfile pattern from src/utils/teammateMailbox.ts.

Events:
  roster  — UserPromptSubmit: register self, read peers, output roster
  touch   — PostToolUse (Edit/Write): update files list in team file
  cleanup — SessionEnd: remove self from team file
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
# File locking
# ---------------------------------------------------------------------------

_ensured_dirs: set[str] = set()


def locked_read_modify_write(path: Path, updater, default=None):
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
# /tmp liveness detection
# ---------------------------------------------------------------------------

def get_all_live_sessions() -> dict[str, set[str]]:
    """Scan /tmp for all live Claude Code sessions. Single scan, reuse the result."""
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


def flatten_live_ids(all_live: dict[str, set[str]]) -> set[str]:
    ids: set[str] = set()
    for s in all_live.values():
        ids.update(s)
    return ids


# ---------------------------------------------------------------------------
# Team file operations
# ---------------------------------------------------------------------------

def team_file_path(project: str) -> Path:
    return TEAMS_DIR / project / "config.json"


def empty_team(project: str) -> dict:
    return {"name": project, "createdAt": int(time.time()), "members": []}


# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------

def read_unread_messages(session_id: str) -> list[dict]:
    p = MAILBOX_DIR / f"{session_id}.json"
    try:
        return [m for m in json.loads(p.read_text()) if not m.get("read")]
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return []


def mark_messages_read(session_id: str) -> None:
    """Mark all messages as read (not deleted — matches Anthropic's pattern)."""
    def updater(messages):
        for m in messages:
            m["read"] = True
        return messages
    locked_read_modify_write(MAILBOX_DIR / f"{session_id}.json", updater, default=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_session_id(payload: dict) -> str:
    sid = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID", "")
    if sid:
        return sid
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
    """UserPromptSubmit: register self in team file, output roster."""
    session_id = get_session_id(payload)
    if not session_id:
        log("[cc] no session_id, skipping")
        return

    cwd = get_cwd(payload)
    project = get_project(cwd)
    encoded = encode_cwd(cwd)
    ts = now_iso()

    # Single /tmp scan — reused for liveness, unregistered detection, and cross-project
    all_live = get_all_live_sessions()
    live_ids = flatten_live_ids(all_live)

    def updater(team):
        if team is None:
            team = empty_team(project)
        team["members"] = [m for m in team["members"] if m.get("agentId") in live_ids]
        self_member = next((m for m in team["members"] if m["agentId"] == session_id), None)
        prompt = payload.get("user_prompt", "")
        task = prompt[:120] if prompt else (self_member or {}).get("task", "")

        if self_member:
            self_member["task"] = task
            self_member["isActive"] = True
        else:
            branch = get_branch(cwd)
            name = make_name(project, branch, session_id, team["members"])
            team["members"].append({
                "agentId": session_id, "name": name, "cwd": cwd,
                "branch": branch, "files": [], "task": task,
                "isActive": True, "joinedAt": int(time.time()),
            })
        return team

    team = locked_read_modify_write(team_file_path(project), updater, default=lambda: empty_team(project))

    peers = [m for m in team["members"] if m["agentId"] != session_id]

    # Detect live sessions on same project that haven't registered yet
    live_here = all_live.get(encoded, set())
    known_ids = {m["agentId"] for m in team["members"]}
    for _ in live_here - known_ids:
        peers.append({"name": f"{project}-?", "branch": "", "files": [],
                       "task": "(just started — no metadata yet)", "isActive": True})

    messages = read_unread_messages(session_id)

    # Cross-project count (reuses all_live — no second scan)
    other_project_count = sum(
        len(sids) for enc, sids in all_live.items()
        if enc != encoded and enc.startswith("-")
    )

    if not peers and not messages and other_project_count == 0:
        return

    self_member = next((m for m in team["members"] if m["agentId"] == session_id), None)
    my_files = set((self_member or {}).get("files", []))
    my_branch = (self_member or {}).get("branch", "")

    if peers:
        context(f"[cc] {len(peers) + 1} sessions on '{project}'")
        for peer in peers:
            peer_branch = peer.get("branch", "")
            peer_files = peer.get("files", [])
            peer_task = peer.get("task", "")
            branch_tag = f" ({peer_branch})" if peer_branch and peer_branch != my_branch else ""
            files_str = ", ".join(peer_files[-3:]) if peer_files else "no files yet"
            task_str = f' — "{peer_task[:60]}"' if peer_task else ""
            context(f"  -> {peer.get('name', '?')}{branch_tag} editing: {files_str}{task_str}")
            for cf in my_files & set(peer_files):
                context(f"  !! {peer.get('name', '?')} is also touching {cf}")

    if other_project_count > 0:
        other_projects = []
        for enc, sids in all_live.items():
            if enc != encoded and enc.startswith("-"):
                parts = enc.strip("-").split("-")
                other_projects.append(f"{parts[-1]}({len(sids)})" if parts else f"?({len(sids)})")
        if other_projects:
            context(f"[cc] also active: {', '.join(other_projects[:5])}")

    if messages:
        context(f"[cc] {len(messages)} message(s):")
        for msg in messages:
            context(f"  <- {msg.get('from', '?')}: {msg.get('text', msg.get('content', ''))[:200]}")
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

    cwd = get_cwd(payload)
    project = get_project(cwd)

    def updater(team):
        if not team:
            return team
        team["members"] = [m for m in team.get("members", []) if m.get("agentId") != session_id]
        return team

    locked_read_modify_write(team_file_path(project), updater)
    log(f"[cc] cleaned up session {session_id[:8]}")


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
