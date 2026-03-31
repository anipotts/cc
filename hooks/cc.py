#!/usr/bin/env python3
"""cc.py — multi-session awareness for Claude Code.

Liveness detection uses Claude Code's own /tmp/claude-{uid}/ directories.
These exist only while a session is running — instant, accurate, zero config.
Metadata (name, files, task) is enriched via ~/.claude/cc/sessions/.

Events:
  roster  — UserPromptSubmit: register self, read peers, output status
  touch   — PostToolUse (Edit/Write): update files list
  cleanup — SessionEnd: remove own session file

stdout = visible to Claude as context
stderr = debug logging only
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "cc" / "sessions"
MAILBOX_DIR = Path.home() / ".claude" / "cc" / "mailbox"
TMP_BASE = Path(f"/tmp/claude-{os.getuid()}")
MAX_TRACKED_FILES = 20


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def context(msg: str) -> None:
    print(msg)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode_cwd(cwd: str) -> str:
    """Encode a cwd path the same way Claude Code does for /tmp dirs."""
    return cwd.replace("/", "-")


def get_all_live_sessions() -> dict[str, set[str]]:
    """Scan /tmp for ALL live Claude Code sessions. Returns {encoded_cwd: {session_ids}}."""
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


def get_session_id(payload: dict) -> str:
    return payload.get("session_id", os.environ.get("CLAUDE_SESSION_ID", ""))


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


def make_name(project: str, branch: str, session_id: str, existing: list[dict]) -> str:
    """Auto-generate a session name: project-branch or project-N."""
    base = f"{project}-{branch}" if branch and branch != "main" else project
    taken = {s.get("name", "") for s in existing if s.get("id") != session_id}
    if base not in taken:
        return base
    for i in range(2, 100):
        candidate = f"{base}-{i}"
        if candidate not in taken:
            return candidate
    return f"{base}-{session_id[:6]}"


def sanitize_id(session_id: str) -> str:
    """Strip path traversal and unsafe chars from session ID."""
    safe = session_id.replace("/", "").replace("\\", "").replace("..", "").replace("\x00", "")
    return safe or "unknown"


def session_path(session_id: str) -> Path:
    return SESSIONS_DIR / f"{sanitize_id(session_id)}.json"


def read_session(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def read_all_sessions(exclude_id: str = "") -> tuple[list[dict], dict[str, set[str]]]:
    """Read all live session metadata, using /tmp for liveness.

    Returns (sessions, all_live) where all_live is the /tmp scan result,
    so callers can reuse it without re-scanning.
    """
    all_live = get_all_live_sessions()

    if not SESSIONS_DIR.exists():
        return [], all_live

    live_ids: set[str] = set()
    for ids in all_live.values():
        live_ids.update(ids)

    sessions = []
    for f in SESSIONS_DIR.glob("*.json"):
        # Skip JSON parse for dead sessions — check filename against live set
        if f.stem not in live_ids:
            log(f"[cc] pruning dead session {f.stem}")
            try:
                f.unlink()
            except OSError:
                pass
            continue
        if f.stem == exclude_id:
            continue
        data = read_session(f)
        if data:
            sessions.append(data)
    return sessions, all_live


def read_mailbox(session_id: str) -> list[dict]:
    """Read and consume messages for this session.

    Note: not concurrency-safe. Claude Code sessions are single-threaded
    so duplicate reads won't happen in practice.
    """
    box = MAILBOX_DIR / session_id
    if not box.exists():
        return []
    messages = []
    for f in sorted(box.glob("*.json")):
        try:
            messages.append(json.loads(f.read_text()))
            f.unlink()
        except (json.JSONDecodeError, OSError):
            pass
    try:
        box.rmdir()
    except OSError:
        pass
    return messages


def write_message(target_id: str, from_name: str, content: str) -> bool:
    """Write a message to another session's mailbox.

    Called by the /cc command (via Claude executing bash), not by hooks directly.
    Kept here as the canonical mailbox write implementation.
    """
    box = MAILBOX_DIR / target_id
    box.mkdir(parents=True, exist_ok=True)
    msg = {
        "from": from_name,
        "content": content,
        "timestamp": now_iso(),
    }
    msg_file = box / f"{int(time.time() * 1000)}.json"
    try:
        msg_file.write_text(json.dumps(msg))
        return True
    except OSError:
        return False


def relative_time(iso_str: str) -> str:
    """Convert ISO timestamp to relative time like '5m ago'.

    Assumes input is UTC with Z suffix (matches now_iso() output).
    """
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
    """UserPromptSubmit: register self, read peers, output roster."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    session_id = get_session_id(payload)
    if not session_id:
        log("[cc] no session_id, skipping")
        return

    cwd = get_cwd(payload)
    project = get_project(cwd)
    ts = now_iso()

    peers, all_live = read_all_sessions(exclude_id=session_id)

    existing_self = read_session(session_path(session_id))

    # Cache branch: only call git on first registration
    if existing_self:
        branch = existing_self.get("branch", "")
    else:
        branch = get_branch(cwd)

    name = (existing_self or {}).get("name") or make_name(project, branch, session_id, peers)
    files = (existing_self or {}).get("files", [])
    started = (existing_self or {}).get("started", ts)

    prompt = payload.get("user_prompt", "")
    task = prompt[:120] if prompt else (existing_self or {}).get("task", "")

    state = {
        "id": session_id,
        "cwd": cwd,
        "project": project,
        "branch": branch,
        "name": name,
        "started": started,
        "updated": ts,
        "task": task,
        "files": files,
    }
    try:
        session_path(session_id).write_text(json.dumps(state))
    except OSError as e:
        log(f"[cc] failed to write state: {e}")
        return

    project_peers = [p for p in peers if p.get("project") == project]

    # Detect live sessions that haven't registered metadata yet (reuse all_live scan)
    encoded = encode_cwd(cwd)
    live_here = all_live.get(encoded, set())
    known_ids = {p.get("id") for p in project_peers} | {session_id}
    unregistered = live_here - known_ids
    for _ in unregistered:
        project_peers.append({
            "name": f"{project}-?",
            "branch": "",
            "files": [],
            "task": "(just started — no metadata yet)",
            "updated": ts,
        })

    messages = read_mailbox(session_id)

    if not project_peers and not messages:
        return

    if project_peers:
        context(f"[cc] {len(project_peers) + 1} sessions active on '{project}'")

        my_files = set(files)

        for peer in project_peers:
            peer_name = peer.get("name", "?")
            peer_branch = peer.get("branch", "")
            peer_files = peer.get("files", [])
            peer_task = peer.get("task", "")
            peer_updated = relative_time(peer.get("updated", ""))

            branch_tag = f" ({peer_branch})" if peer_branch and peer_branch != branch else ""
            files_str = ", ".join(peer_files[-3:]) if peer_files else "no files yet"
            task_str = f' — "{peer_task[:60]}"' if peer_task else ""

            context(f"  -> {peer_name}{branch_tag} editing: {files_str}{task_str} — {peer_updated}")

            conflicts = my_files & set(peer_files)
            for cf in conflicts:
                context(f"  !! {peer_name} is also touching {cf}")

    if messages:
        context(f"[cc] {len(messages)} message(s) for you:")
        for msg in messages:
            context(f"  <- {msg.get('from', '?')}: {msg.get('content', '')[:200]}")


def handle_touch(payload: dict) -> None:
    """PostToolUse (Edit/Write): update files list."""
    session_id = get_session_id(payload)
    if not session_id:
        return

    data = read_session(session_path(session_id))
    if not data:
        return

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    cwd = data.get("cwd", "")
    if cwd and file_path.startswith(cwd):
        file_path = file_path[len(cwd):].lstrip("/")

    files = data.get("files", [])
    if file_path not in files:
        files.append(file_path)
        if len(files) > MAX_TRACKED_FILES:
            files = files[-MAX_TRACKED_FILES:]
        data["files"] = files
        data["updated"] = now_iso()
        try:
            session_path(session_id).write_text(json.dumps(data))
        except OSError:
            pass


def handle_cleanup(payload: dict) -> None:
    """SessionEnd: remove own session file."""
    session_id = get_session_id(payload)
    if not session_id:
        return
    try:
        session_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass
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
