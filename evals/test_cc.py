#!/usr/bin/env python3
"""Comprehensive test suite for cc.py — multi-session awareness hook.

Tests all 3 handlers (roster, touch, cleanup) with edge cases:
- Normal operation with /tmp liveness detection
- Missing/empty payloads
- Corrupted JSON in session files
- Path traversal in session IDs
- Dead session pruning via /tmp
- Concurrent session scenarios
- Mailbox messaging
- File conflict detection
- Name auto-generation collisions
- Large file lists (>20 files)
- Unicode in task descriptions
- Unregistered live session detection
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOOK_SCRIPT = Path(__file__).parent.parent / "hooks" / "cc.py"
SESSIONS_DIR = Path.home() / ".claude" / "cc" / "sessions"
MAILBOX_DIR = Path.home() / ".claude" / "cc" / "mailbox"
TMP_BASE = Path(f"/tmp/claude-{os.getuid()}")

# Use a test project path that won't collide with real sessions
TEST_CWD = "/tmp/cc-test-project"
TEST_ENCODED = TEST_CWD.replace("/", "-")

passed = 0
failed = 0
errors = []


def run_hook(event: str, payload: dict) -> tuple[str, str, int]:
    """Run cc.py with a given event and payload."""
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), event],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout, result.stderr, result.returncode


def clean_sessions():
    """Remove all session files, mailbox, and test /tmp dirs."""
    if SESSIONS_DIR.exists():
        shutil.rmtree(SESSIONS_DIR)
    if MAILBOX_DIR.exists():
        shutil.rmtree(MAILBOX_DIR)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    clean_tmp()


def clean_tmp():
    """Remove test /tmp directories."""
    test_tmp = TMP_BASE / TEST_ENCODED
    if test_tmp.exists():
        shutil.rmtree(test_tmp)


def create_tmp_session(session_id: str, cwd: str = TEST_CWD):
    """Create a /tmp directory to simulate a live Claude Code session."""
    encoded = cwd.replace("/", "-")
    tmp_dir = TMP_BASE / encoded / session_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir


def write_fake_session(session_id: str, data: dict):
    """Write a fake session metadata file."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSIONS_DIR / f"{session_id}.json"
    path.write_text(json.dumps(data))


def read_session_file(session_id: str) -> dict | None:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def assert_test(name: str, condition: bool, detail: str = ""):
    global passed, failed, errors
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        errors.append(msg)


# ===========================================================================
# TEST GROUP 1: Roster Handler
# ===========================================================================

def test_roster_basic():
    """Roster creates session file with correct fields."""
    clean_sessions()
    create_tmp_session("test-001")
    payload = {
        "session_id": "test-001",
        "cwd": TEST_CWD,
        "user_prompt": "fix the login bug",
    }
    stdout, stderr, code = run_hook("roster", payload)
    assert_test("roster:basic:exits_0", code == 0)

    data = read_session_file("test-001")
    assert_test("roster:basic:file_created", data is not None)
    assert_test("roster:basic:has_id", data.get("id") == "test-001")
    assert_test("roster:basic:has_project", data.get("project") == "cc-test-project")
    assert_test("roster:basic:has_task", data.get("task") == "fix the login bug")
    assert_test("roster:basic:has_started", "started" in data)
    assert_test("roster:basic:has_updated", "updated" in data)
    assert_test("roster:basic:has_files", isinstance(data.get("files"), list))
    assert_test("roster:basic:no_stdout_when_alone", stdout.strip() == "",
                f"expected empty, got: {stdout[:100]}")


def test_roster_no_session_id():
    """Roster with no session_id should not create a file."""
    clean_sessions()
    stdout, stderr, code = run_hook("roster", {"cwd": TEST_CWD})
    assert_test("roster:no_id:exits_0", code == 0)
    files = list(SESSIONS_DIR.glob("*.json"))
    assert_test("roster:no_id:no_files_created", len(files) == 0)


def test_roster_empty_payload():
    """Roster with empty payload should not crash."""
    clean_sessions()
    stdout, stderr, code = run_hook("roster", {})
    assert_test("roster:empty:exits_0", code == 0)


def test_roster_shows_peers():
    """Roster shows other sessions in same project."""
    clean_sessions()
    # Create live peer in /tmp
    create_tmp_session("peer-001")
    write_fake_session("peer-001", {
        "id": "peer-001",
        "cwd": TEST_CWD,
        "project": "cc-test-project",
        "branch": "main",
        "name": "cc-test-project",
        "started": "2026-03-31T05:00:00Z",
        "updated": "2026-03-31T05:30:00Z",
        "task": "writing tests",
        "files": ["src/auth.ts"],
    })
    # Create our session in /tmp
    create_tmp_session("test-002")
    payload = {
        "session_id": "test-002",
        "cwd": TEST_CWD,
        "user_prompt": "refactor auth",
    }
    stdout, stderr, code = run_hook("roster", payload)
    assert_test("roster:peers:exits_0", code == 0)
    assert_test("roster:peers:shows_count", "2 sessions active" in stdout,
                f"stdout: {stdout[:200]}")
    assert_test("roster:peers:shows_peer_name", "cc-test-project" in stdout)
    assert_test("roster:peers:shows_files", "src/auth.ts" in stdout)
    assert_test("roster:peers:shows_task", "writing tests" in stdout)


def test_roster_file_conflict():
    """Roster detects file conflicts between sessions."""
    clean_sessions()
    create_tmp_session("peer-conflict")
    write_fake_session("peer-conflict", {
        "id": "peer-conflict",
        "cwd": TEST_CWD,
        "project": "cc-test-project",
        "branch": "main",
        "name": "cc-test-project",
        "started": "2026-03-31T05:00:00Z",
        "updated": "2026-03-31T05:30:00Z",
        "task": "editing hook",
        "files": ["hooks/cc.py"],
    })
    create_tmp_session("test-conflict")
    write_fake_session("test-conflict", {
        "id": "test-conflict",
        "cwd": TEST_CWD,
        "project": "cc-test-project",
        "branch": "main",
        "name": "cc-test-project-2",
        "started": "2026-03-31T05:00:00Z",
        "updated": "2026-03-31T05:30:00Z",
        "task": "also editing hook",
        "files": ["hooks/cc.py"],
    })
    payload = {
        "session_id": "test-conflict",
        "cwd": TEST_CWD,
        "user_prompt": "check conflicts",
    }
    stdout, stderr, code = run_hook("roster", payload)
    assert_test("roster:conflict:exits_0", code == 0)
    assert_test("roster:conflict:detected", "!!" in stdout and "hooks/cc.py" in stdout,
                f"stdout: {stdout[:300]}")


def test_roster_cross_project_isolation():
    """Roster does NOT show sessions from different projects."""
    clean_sessions()
    create_tmp_session("other-project", cwd="/tmp/other-project")
    write_fake_session("other-project", {
        "id": "other-project",
        "cwd": "/tmp/other-project",
        "project": "other-project",
        "branch": "main",
        "name": "other",
        "started": "2026-03-31T05:00:00Z",
        "updated": "2026-03-31T05:30:00Z",
        "task": "working on other",
        "files": [],
    })
    create_tmp_session("test-isolated")
    payload = {
        "session_id": "test-isolated",
        "cwd": TEST_CWD,
        "user_prompt": "check roster",
    }
    stdout, stderr, code = run_hook("roster", payload)
    assert_test("roster:isolation:exits_0", code == 0)
    assert_test("roster:isolation:no_output", stdout.strip() == "",
                f"should not show other project, got: {stdout[:200]}")
    # Clean up the other project tmp
    other_tmp = TMP_BASE / "-tmp-other-project"
    if other_tmp.exists():
        shutil.rmtree(other_tmp)


def test_roster_preserves_files_across_calls():
    """Roster preserves the files list from previous calls."""
    clean_sessions()
    create_tmp_session("test-preserve")
    run_hook("roster", {"session_id": "test-preserve", "cwd": TEST_CWD, "user_prompt": "a"})
    run_hook("touch", {"session_id": "test-preserve", "tool_input": {"file_path": f"{TEST_CWD}/foo.py"}})
    run_hook("roster", {"session_id": "test-preserve", "cwd": TEST_CWD, "user_prompt": "b"})
    data = read_session_file("test-preserve")
    assert_test("roster:preserves_files", "foo.py" in (data or {}).get("files", []),
                f"files: {(data or {}).get('files')}")


def test_roster_name_autogeneration():
    """Auto-generated names are unique across sessions."""
    clean_sessions()
    create_tmp_session("first")
    write_fake_session("first", {
        "id": "first", "cwd": TEST_CWD,
        "project": "cc-test-project", "branch": "main", "name": "cc-test-project",
        "started": "2026-03-31T05:00:00Z", "updated": "2026-03-31T05:30:00Z",
        "task": "", "files": [],
    })
    create_tmp_session("second")
    run_hook("roster", {"session_id": "second", "cwd": TEST_CWD, "user_prompt": "x"})
    data = read_session_file("second")
    assert_test("roster:name:unique", data is not None and data.get("name") != "cc-test-project",
                f"name: {(data or {}).get('name')}")
    assert_test("roster:name:proj-2", data is not None and data.get("name") == "cc-test-project-2",
                f"name: {(data or {}).get('name')}")


def test_roster_long_prompt_truncation():
    """Task field truncates long prompts to 120 chars."""
    clean_sessions()
    create_tmp_session("long-prompt")
    long_prompt = "x" * 200
    run_hook("roster", {"session_id": "long-prompt", "cwd": TEST_CWD, "user_prompt": long_prompt})
    data = read_session_file("long-prompt")
    assert_test("roster:truncation", len((data or {}).get("task", "")) <= 120)


def test_roster_unicode_prompt():
    """Handles unicode in prompts."""
    clean_sessions()
    create_tmp_session("unicode-test")
    run_hook("roster", {
        "session_id": "unicode-test",
        "cwd": TEST_CWD,
        "user_prompt": "fix the bug in 日本語ファイル.py 🐛",
    })
    data = read_session_file("unicode-test")
    assert_test("roster:unicode:created", data is not None)
    assert_test("roster:unicode:preserved", "日本語" in (data or {}).get("task", ""))


def test_roster_detects_unregistered_sessions():
    """Roster shows live sessions that haven't registered metadata yet."""
    clean_sessions()
    # Create a live session in /tmp but don't write metadata
    create_tmp_session("unregistered-001")
    # Create our session
    create_tmp_session("test-unreg")
    payload = {
        "session_id": "test-unreg",
        "cwd": TEST_CWD,
        "user_prompt": "check who's here",
    }
    stdout, stderr, code = run_hook("roster", payload)
    assert_test("roster:unregistered:exits_0", code == 0)
    assert_test("roster:unregistered:detected", "sessions active" in stdout,
                f"stdout: {stdout[:200]}")
    assert_test("roster:unregistered:shows_placeholder", "no metadata yet" in stdout or "just started" in stdout,
                f"stdout: {stdout[:200]}")


# ===========================================================================
# TEST GROUP 2: Touch Handler
# ===========================================================================

def test_touch_basic():
    """Touch adds file to session's files list."""
    clean_sessions()
    create_tmp_session("touch-test")
    run_hook("roster", {"session_id": "touch-test", "cwd": TEST_CWD, "user_prompt": "work"})
    run_hook("touch", {"session_id": "touch-test", "tool_input": {"file_path": f"{TEST_CWD}/src/app.ts"}})
    data = read_session_file("touch-test")
    assert_test("touch:basic:added", "src/app.ts" in (data or {}).get("files", []),
                f"files: {(data or {}).get('files')}")


def test_touch_relative_path():
    """Touch converts absolute paths to relative."""
    clean_sessions()
    create_tmp_session("touch-rel")
    run_hook("roster", {"session_id": "touch-rel", "cwd": "/Users/test/project", "user_prompt": "x"})
    run_hook("touch", {"session_id": "touch-rel", "tool_input": {"file_path": "/Users/test/project/lib/utils.py"}})
    data = read_session_file("touch-rel")
    files = (data or {}).get("files", [])
    assert_test("touch:relative:converted", "lib/utils.py" in files, f"files: {files}")
    assert_test("touch:relative:no_absolute", not any(f.startswith("/") for f in files))


def test_touch_deduplication():
    """Touch does not add the same file twice."""
    clean_sessions()
    create_tmp_session("touch-dedup")
    run_hook("roster", {"session_id": "touch-dedup", "cwd": TEST_CWD, "user_prompt": "x"})
    run_hook("touch", {"session_id": "touch-dedup", "tool_input": {"file_path": f"{TEST_CWD}/a.py"}})
    run_hook("touch", {"session_id": "touch-dedup", "tool_input": {"file_path": f"{TEST_CWD}/a.py"}})
    data = read_session_file("touch-dedup")
    files = (data or {}).get("files", [])
    assert_test("touch:dedup", files.count("a.py") == 1, f"files: {files}")


def test_touch_max_files():
    """Touch caps file list at 20."""
    clean_sessions()
    create_tmp_session("touch-max")
    run_hook("roster", {"session_id": "touch-max", "cwd": TEST_CWD, "user_prompt": "x"})
    for i in range(25):
        run_hook("touch", {"session_id": "touch-max", "tool_input": {"file_path": f"{TEST_CWD}/file{i}.py"}})
    data = read_session_file("touch-max")
    files = (data or {}).get("files", [])
    assert_test("touch:max:capped", len(files) <= 20, f"got {len(files)} files")
    assert_test("touch:max:keeps_recent", "file24.py" in files, f"files: {files[-5:]}")


def test_touch_no_session():
    """Touch with no existing session file does nothing."""
    clean_sessions()
    stdout, stderr, code = run_hook("touch", {
        "session_id": "nonexistent",
        "tool_input": {"file_path": "/tmp/foo.py"},
    })
    assert_test("touch:no_session:exits_0", code == 0)
    assert_test("touch:no_session:no_file", read_session_file("nonexistent") is None)


def test_touch_no_file_path():
    """Touch with missing file_path does nothing."""
    clean_sessions()
    create_tmp_session("touch-nofp")
    run_hook("roster", {"session_id": "touch-nofp", "cwd": TEST_CWD, "user_prompt": "x"})
    run_hook("touch", {"session_id": "touch-nofp", "tool_input": {}})
    data = read_session_file("touch-nofp")
    assert_test("touch:no_filepath:empty", (data or {}).get("files") == [])


def test_touch_empty_payload():
    """Touch with empty payload does not crash."""
    clean_sessions()
    stdout, stderr, code = run_hook("touch", {})
    assert_test("touch:empty:exits_0", code == 0)


def test_touch_updates_timestamp():
    """Touch updates the 'updated' field."""
    clean_sessions()
    create_tmp_session("touch-ts")
    run_hook("roster", {"session_id": "touch-ts", "cwd": TEST_CWD, "user_prompt": "x"})
    data1 = read_session_file("touch-ts")
    time.sleep(1.1)
    run_hook("touch", {"session_id": "touch-ts", "tool_input": {"file_path": f"{TEST_CWD}/new.py"}})
    data2 = read_session_file("touch-ts")
    assert_test("touch:timestamp:updated",
                (data2 or {}).get("updated") != (data1 or {}).get("updated"),
                f"before: {(data1 or {}).get('updated')} after: {(data2 or {}).get('updated')}")


# ===========================================================================
# TEST GROUP 3: Cleanup Handler
# ===========================================================================

def test_cleanup_basic():
    """Cleanup removes the session file."""
    clean_sessions()
    create_tmp_session("cleanup-test")
    run_hook("roster", {"session_id": "cleanup-test", "cwd": TEST_CWD, "user_prompt": "x"})
    assert_test("cleanup:basic:exists_before", read_session_file("cleanup-test") is not None)
    run_hook("cleanup", {"session_id": "cleanup-test"})
    assert_test("cleanup:basic:removed", read_session_file("cleanup-test") is None)


def test_cleanup_nonexistent():
    """Cleanup on nonexistent session does not crash."""
    clean_sessions()
    stdout, stderr, code = run_hook("cleanup", {"session_id": "does-not-exist"})
    assert_test("cleanup:nonexistent:exits_0", code == 0)


def test_cleanup_empty_payload():
    """Cleanup with empty payload does not crash."""
    clean_sessions()
    stdout, stderr, code = run_hook("cleanup", {})
    assert_test("cleanup:empty:exits_0", code == 0)


# ===========================================================================
# TEST GROUP 4: Security — Path Traversal
# ===========================================================================

def test_security_path_traversal_session_id():
    """Session IDs with path traversal should not escape sessions dir."""
    clean_sessions()
    malicious_id = "../../../etc/passwd"
    stdout, stderr, code = run_hook("roster", {
        "session_id": malicious_id,
        "cwd": TEST_CWD,
        "user_prompt": "x",
    })
    assert_test("security:traversal:exits_0", code == 0)
    assert_test("security:traversal:contained",
                not Path("/Users/anipotts/.claude/cc/etc/passwd.json").exists() and
                not Path("/etc/passwd.json").exists())


# ===========================================================================
# TEST GROUP 5: Robustness — Corrupted Data
# ===========================================================================

def test_robustness_corrupted_json():
    """Corrupted session files should be skipped, not crash."""
    clean_sessions()
    create_tmp_session("robust-test")
    (SESSIONS_DIR / "corrupted.json").write_text("{invalid json!!!")
    run_hook("roster", {"session_id": "robust-test", "cwd": TEST_CWD, "user_prompt": "x"})
    data = read_session_file("robust-test")
    assert_test("robustness:corrupted:still_works", data is not None)


def test_robustness_empty_session_file():
    """Empty session file should be skipped."""
    clean_sessions()
    create_tmp_session("robust-empty")
    (SESSIONS_DIR / "empty.json").write_text("")
    stdout, stderr, code = run_hook("roster", {"session_id": "robust-empty", "cwd": TEST_CWD, "user_prompt": "x"})
    assert_test("robustness:empty_file:exits_0", code == 0)


def test_robustness_missing_fields():
    """Session file with missing fields should not crash peers."""
    clean_sessions()
    create_tmp_session("minimal")
    write_fake_session("minimal", {"id": "minimal"})
    create_tmp_session("robust-minimal")
    stdout, stderr, code = run_hook("roster", {
        "session_id": "robust-minimal",
        "cwd": TEST_CWD,
        "user_prompt": "x",
    })
    assert_test("robustness:missing_fields:exits_0", code == 0)


def test_robustness_huge_task():
    """Very long task string in session file should not crash."""
    clean_sessions()
    create_tmp_session("huge-task")
    write_fake_session("huge-task", {
        "id": "huge-task",
        "cwd": TEST_CWD, "project": "cc-test-project", "branch": "main",
        "name": "huge", "started": "2026-03-31T05:00:00Z",
        "updated": "2026-03-31T05:30:00Z",
        "task": "A" * 10000, "files": [],
    })
    create_tmp_session("robust-huge")
    stdout, stderr, code = run_hook("roster", {
        "session_id": "robust-huge",
        "cwd": TEST_CWD,
        "user_prompt": "x",
    })
    assert_test("robustness:huge_task:exits_0", code == 0)
    assert_test("robustness:huge_task:truncated", len(stdout) < 11000,
                f"stdout length: {len(stdout)}")


# ===========================================================================
# TEST GROUP 6: Dead Session Pruning via /tmp
# ===========================================================================

def test_dead_session_pruned():
    """Sessions without /tmp directories are pruned from metadata."""
    clean_sessions()
    # Write metadata for a session but DON'T create its /tmp dir
    write_fake_session("dead-session", {
        "id": "dead-session",
        "cwd": TEST_CWD,
        "project": "cc-test-project",
        "branch": "main",
        "name": "dead",
        "started": "2026-03-31T05:00:00Z",
        "updated": "2026-03-31T05:30:00Z",
        "task": "ghost",
        "files": [],
    })
    # Create our live session
    create_tmp_session("alive-test")
    run_hook("roster", {"session_id": "alive-test", "cwd": TEST_CWD, "user_prompt": "x"})
    # Dead session's metadata should be cleaned up
    assert_test("dead:pruned", read_session_file("dead-session") is None)
    assert_test("dead:alive_exists", read_session_file("alive-test") is not None)


def test_dead_not_shown_in_roster():
    """Dead sessions should not appear in roster output."""
    clean_sessions()
    write_fake_session("ghost", {
        "id": "ghost",
        "cwd": TEST_CWD,
        "project": "cc-test-project",
        "branch": "main",
        "name": "ghost",
        "started": "2026-03-31T05:00:00Z",
        "updated": "2026-03-31T05:30:00Z",
        "task": "i am dead",
        "files": [],
    })
    create_tmp_session("living")
    stdout, stderr, code = run_hook("roster", {
        "session_id": "living",
        "cwd": TEST_CWD,
        "user_prompt": "x",
    })
    assert_test("dead:not_in_output", "ghost" not in stdout, f"stdout: {stdout[:200]}")


# ===========================================================================
# TEST GROUP 7: Mailbox
# ===========================================================================

def test_mailbox_send_and_receive():
    """Messages sent to a session are received on next roster call."""
    clean_sessions()
    create_tmp_session("mail-receiver")
    run_hook("roster", {"session_id": "mail-receiver", "cwd": TEST_CWD, "user_prompt": "x"})
    box = MAILBOX_DIR / "mail-receiver"
    box.mkdir(parents=True, exist_ok=True)
    msg = {"from": "sender", "content": "hey update your imports", "timestamp": "2026-03-31T05:30:00Z"}
    (box / "1.json").write_text(json.dumps(msg))
    stdout, stderr, code = run_hook("roster", {
        "session_id": "mail-receiver",
        "cwd": TEST_CWD,
        "user_prompt": "check mail",
    })
    assert_test("mailbox:received", "hey update your imports" in stdout, f"stdout: {stdout[:300]}")
    assert_test("mailbox:from_shown", "sender" in stdout)


def test_mailbox_consumed_after_read():
    """Messages are deleted after being read."""
    clean_sessions()
    create_tmp_session("mail-consume")
    run_hook("roster", {"session_id": "mail-consume", "cwd": TEST_CWD, "user_prompt": "x"})
    box = MAILBOX_DIR / "mail-consume"
    box.mkdir(parents=True, exist_ok=True)
    (box / "1.json").write_text(json.dumps({"from": "a", "content": "test", "timestamp": "now"}))
    stdout1, _, _ = run_hook("roster", {"session_id": "mail-consume", "cwd": TEST_CWD, "user_prompt": "x"})
    stdout2, _, _ = run_hook("roster", {"session_id": "mail-consume", "cwd": TEST_CWD, "user_prompt": "x"})
    assert_test("mailbox:consumed:first_read", "test" in stdout1)
    assert_test("mailbox:consumed:second_read", "test" not in stdout2, f"stdout2: {stdout2[:200]}")


# ===========================================================================
# TEST GROUP 8: Dispatcher
# ===========================================================================

def test_unknown_event():
    stdout, stderr, code = run_hook("bogus", {})
    assert_test("dispatcher:unknown:exits_nonzero", code != 0)


def test_no_event():
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        capture_output=True, text=True, timeout=5,
    )
    assert_test("dispatcher:no_event:exits_nonzero", result.returncode != 0)
    assert_test("dispatcher:no_event:shows_usage", "usage" in result.stderr.lower())


def test_invalid_json_stdin():
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), "roster"],
        input="not json at all {{{",
        capture_output=True, text=True, timeout=5,
    )
    assert_test("dispatcher:bad_json:exits_0", result.returncode == 0)


def test_empty_stdin():
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), "roster"],
        input="",
        capture_output=True, text=True, timeout=5,
    )
    assert_test("dispatcher:empty_stdin:exits_0", result.returncode == 0)


# ===========================================================================
# TEST GROUP 9: Performance
# ===========================================================================

def test_performance_roster():
    """Roster should complete in under 2 seconds with 10 peers."""
    clean_sessions()
    for i in range(10):
        sid = f"perf-{i}"
        create_tmp_session(sid)
        write_fake_session(sid, {
            "id": sid,
            "cwd": TEST_CWD, "project": "cc-test-project", "branch": "main",
            "name": f"cc-test-project-{i}", "started": "2026-03-31T05:00:00Z",
            "updated": "2026-03-31T05:30:00Z",
            "task": f"task {i}", "files": [f"file{j}.py" for j in range(5)],
        })
    create_tmp_session("perf-test")
    start = time.time()
    run_hook("roster", {"session_id": "perf-test", "cwd": TEST_CWD, "user_prompt": "x"})
    elapsed = time.time() - start
    assert_test("performance:roster:under_2s", elapsed < 2.0, f"took {elapsed:.2f}s")


# ===========================================================================
# TEST GROUP 10: Edge Cases
# ===========================================================================

def test_special_chars_in_project_name():
    """Project names with special chars should work."""
    clean_sessions()
    cwd = "/tmp/my-project.v2"
    create_tmp_session("special-proj", cwd=cwd)
    run_hook("roster", {"session_id": "special-proj", "cwd": cwd, "user_prompt": "x"})
    data = read_session_file("special-proj")
    assert_test("edge:special_project", data is not None and data.get("project") == "my-project.v2")
    # cleanup
    encoded = cwd.replace("/", "-")
    tmp = TMP_BASE / encoded
    if tmp.exists():
        shutil.rmtree(tmp)


def test_concurrent_roster_calls():
    """Multiple roster calls don't corrupt session files."""
    clean_sessions()
    import concurrent.futures

    def run_one(i):
        sid = f"concurrent-{i}"
        create_tmp_session(sid)
        run_hook("roster", {"session_id": sid, "cwd": TEST_CWD, "user_prompt": f"task {i}"})
        return read_session_file(sid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(run_one, range(5)))

    valid = [r for r in results if r is not None]
    assert_test("edge:concurrent:all_created", len(valid) == 5, f"got {len(valid)}/5")


def test_real_sessions_detected():
    """Can detect actually running Claude Code sessions via /tmp."""
    all_live = {}
    if TMP_BASE.is_dir():
        for d in TMP_BASE.iterdir():
            if d.is_dir() and d.name.startswith("-"):
                sessions = [s.name for s in d.iterdir() if s.is_dir() and "-" in s.name]
                if sessions:
                    all_live[d.name] = sessions

    # We know at least THIS session is running
    our_encoded = encode_cwd(os.getcwd())
    assert_test("edge:real_sessions:tmp_exists", TMP_BASE.is_dir())
    assert_test("edge:real_sessions:found_some", len(all_live) > 0,
                f"found {len(all_live)} project dirs")


def encode_cwd(cwd):
    return cwd.replace("/", "-")


# ===========================================================================
# Run all tests
# ===========================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("cc plugin — comprehensive test suite (v2: /tmp liveness)")
    print("=" * 60)

    tests = [
        # Roster
        test_roster_basic,
        test_roster_no_session_id,
        test_roster_empty_payload,
        test_roster_shows_peers,
        test_roster_file_conflict,
        test_roster_cross_project_isolation,
        test_roster_preserves_files_across_calls,
        test_roster_name_autogeneration,
        test_roster_long_prompt_truncation,
        test_roster_unicode_prompt,
        test_roster_detects_unregistered_sessions,
        # Touch
        test_touch_basic,
        test_touch_relative_path,
        test_touch_deduplication,
        test_touch_max_files,
        test_touch_no_session,
        test_touch_no_file_path,
        test_touch_empty_payload,
        test_touch_updates_timestamp,
        # Cleanup
        test_cleanup_basic,
        test_cleanup_nonexistent,
        test_cleanup_empty_payload,
        # Security
        test_security_path_traversal_session_id,
        # Robustness
        test_robustness_corrupted_json,
        test_robustness_empty_session_file,
        test_robustness_missing_fields,
        test_robustness_huge_task,
        # Dead session pruning
        test_dead_session_pruned,
        test_dead_not_shown_in_roster,
        # Mailbox
        test_mailbox_send_and_receive,
        test_mailbox_consumed_after_read,
        # Dispatcher
        test_unknown_event,
        test_no_event,
        test_invalid_json_stdin,
        test_empty_stdin,
        # Performance
        test_performance_roster,
        # Edge cases
        test_special_chars_in_project_name,
        test_concurrent_roster_calls,
        test_real_sessions_detected,
    ]

    for test_fn in tests:
        print(f"\n--- {test_fn.__name__} ---")
        try:
            test_fn()
        except Exception as e:
            failed += 1
            msg = f"  CRASH {test_fn.__name__}: {e}"
            print(msg)
            errors.append(msg)

    # Cleanup
    clean_sessions()

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed ({passed + failed} total)")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(e)
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
