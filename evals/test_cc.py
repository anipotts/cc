#!/usr/bin/env python3
"""Comprehensive test suite for cc v0.2 — team file + locking + XML roster.

Tests all 3 handlers (roster, touch, cleanup) with:
- Team file read/write with locking
- XML-formatted roster output
- Mailbox with read/unread tracking
- /tmp liveness detection
- Concurrent safety
- Edge cases and robustness
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOOK_SCRIPT = Path(__file__).parent.parent / "hooks" / "cc.py"
CC_DIR = Path.home() / ".claude" / "cc"
TEAMS_DIR = CC_DIR / "teams"
MAILBOX_DIR = CC_DIR / "mailbox"
TMP_BASE = Path(f"/tmp/claude-{os.getuid()}")

TEST_CWD = "/tmp/cc-test-project"
TEST_PROJECT = "cc-test-project"
TEST_ENCODED = TEST_CWD.replace("/", "-")

passed = 0
failed = 0
errors = []


def run_hook(event: str, payload: dict) -> tuple[str, str, int]:
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), event],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout, result.stderr, result.returncode


def clean():
    """Remove all cc state and test /tmp dirs."""
    for d in [TEAMS_DIR, MAILBOX_DIR]:
        if d.exists():
            shutil.rmtree(d)
    test_tmp = TMP_BASE / TEST_ENCODED
    if test_tmp.exists():
        shutil.rmtree(test_tmp)


def create_tmp_session(session_id: str, cwd: str = TEST_CWD):
    encoded = cwd.replace("/", "-")
    (TMP_BASE / encoded / session_id).mkdir(parents=True, exist_ok=True)


def read_team() -> dict | None:
    p = TEAMS_DIR / TEST_PROJECT / "config.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def write_team(data: dict):
    d = TEAMS_DIR / TEST_PROJECT
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(data))


def write_inbox(session_id: str, messages: list):
    MAILBOX_DIR.mkdir(parents=True, exist_ok=True)
    (MAILBOX_DIR / f"{session_id}.json").write_text(json.dumps(messages))


def read_inbox(session_id: str) -> list:
    p = MAILBOX_DIR / f"{session_id}.json"
    if not p.exists():
        return []
    return json.loads(p.read_text())


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
# ROSTER: Basic
# ===========================================================================

def test_roster_basic():
    clean()
    create_tmp_session("test-001")
    payload = {"session_id": "test-001", "cwd": TEST_CWD, "user_prompt": "fix login bug"}
    stdout, stderr, code = run_hook("roster", payload)
    assert_test("roster:basic:exits_0", code == 0)

    team = read_team()
    assert_test("roster:basic:team_created", team is not None)
    assert_test("roster:basic:has_member", len(team.get("members", [])) == 1)
    member = team["members"][0]
    assert_test("roster:basic:has_id", member["agentId"] == "test-001")
    assert_test("roster:basic:has_task", member["task"] == "fix login bug")
    assert_test("roster:basic:has_name", member["name"] == TEST_PROJECT)
    assert_test("roster:basic:is_active", member["isActive"] is True)
    assert_test("roster:basic:no_output_alone", stdout.strip() == "")


def test_roster_no_session_id():
    clean()
    stdout, stderr, code = run_hook("roster", {"cwd": TEST_CWD})
    assert_test("roster:no_id:exits_0", code == 0)
    assert_test("roster:no_id:no_team", read_team() is None)


def test_roster_empty_payload():
    clean()
    stdout, stderr, code = run_hook("roster", {})
    assert_test("roster:empty:exits_0", code == 0)


def test_roster_shows_peers_xml():
    clean()
    create_tmp_session("peer-001")
    write_team({
        "name": TEST_PROJECT, "createdAt": 0,
        "members": [{
            "agentId": "peer-001", "name": TEST_PROJECT, "cwd": TEST_CWD,
            "branch": "main", "files": ["src/auth.ts"], "task": "writing tests",
            "isActive": True, "joinedAt": 0,
        }]
    })
    create_tmp_session("test-002")
    stdout, _, code = run_hook("roster", {"session_id": "test-002", "cwd": TEST_CWD, "user_prompt": "refactor"})
    assert_test("roster:fmt:exits_0", code == 0)
    assert_test("roster:fmt:has_roster_tag", "[cc]" in stdout)
    assert_test("roster:fmt:has_session_tag", "->" in stdout)
    assert_test("roster:fmt:has_peer_name", TEST_PROJECT in stdout)
    assert_test("roster:fmt:has_files", "src/auth.ts" in stdout)
    assert_test("roster:fmt:has_arrow", "->" in stdout)


def test_roster_file_conflict_xml():
    clean()
    create_tmp_session("peer-cf")
    write_team({
        "name": TEST_PROJECT, "createdAt": 0,
        "members": [
            {"agentId": "peer-cf", "name": "peer", "cwd": TEST_CWD,
             "branch": "main", "files": ["hooks/cc.py"], "task": "editing hook",
             "isActive": True, "joinedAt": 0},
            {"agentId": "test-cf", "name": "me", "cwd": TEST_CWD,
             "branch": "main", "files": ["hooks/cc.py"], "task": "also editing",
             "isActive": True, "joinedAt": 0},
        ]
    })
    create_tmp_session("test-cf")
    stdout, _, _ = run_hook("roster", {"session_id": "test-cf", "cwd": TEST_CWD, "user_prompt": "x"})
    assert_test("roster:conflict:xml", "!!" in stdout and "hooks/cc.py" in stdout,
                f"stdout: {stdout[:300]}")


def test_roster_cross_project_isolation():
    clean()
    other_cwd = "/tmp/other-project"
    create_tmp_session("other", cwd=other_cwd)
    # Write to OTHER project's team file (not TEST_PROJECT)
    other_dir = TEAMS_DIR / "other-project"
    other_dir.mkdir(parents=True, exist_ok=True)
    (other_dir / "config.json").write_text(json.dumps({
        "name": "other-project", "createdAt": 0,
        "members": [{"agentId": "other", "name": "other", "cwd": other_cwd,
                      "branch": "main", "files": [], "task": "", "isActive": True, "joinedAt": 0}]
    }))
    create_tmp_session("test-iso")
    stdout, _, _ = run_hook("roster", {"session_id": "test-iso", "cwd": TEST_CWD, "user_prompt": "x"})
    assert_test("roster:isolation:no_output", stdout.strip() == "",
                f"got: {stdout[:100]}")
    # Cleanup
    other_tmp = TMP_BASE / other_cwd.replace("/", "-")
    if other_tmp.exists():
        shutil.rmtree(other_tmp)


def test_roster_name_autogeneration():
    clean()
    create_tmp_session("first")
    write_team({
        "name": TEST_PROJECT, "createdAt": 0,
        "members": [{"agentId": "first", "name": TEST_PROJECT, "cwd": TEST_CWD,
                      "branch": "main", "files": [], "task": "", "isActive": True, "joinedAt": 0}]
    })
    create_tmp_session("second")
    run_hook("roster", {"session_id": "second", "cwd": TEST_CWD, "user_prompt": "x"})
    team = read_team()
    names = [m["name"] for m in team["members"]]
    assert_test("roster:name:unique", len(set(names)) == 2, f"names: {names}")
    assert_test("roster:name:second_suffixed", f"{TEST_PROJECT}-2" in names, f"names: {names}")


def test_roster_dead_session_pruned():
    clean()
    # Write a member but DON'T create /tmp for them
    write_team({
        "name": TEST_PROJECT, "createdAt": 0,
        "members": [{"agentId": "dead", "name": "dead", "cwd": TEST_CWD,
                      "branch": "main", "files": [], "task": "ghost", "isActive": True, "joinedAt": 0}]
    })
    create_tmp_session("alive")
    run_hook("roster", {"session_id": "alive", "cwd": TEST_CWD, "user_prompt": "x"})
    team = read_team()
    ids = [m["agentId"] for m in team["members"]]
    assert_test("roster:prune:dead_removed", "dead" not in ids, f"ids: {ids}")
    assert_test("roster:prune:alive_kept", "alive" in ids)


def test_roster_unregistered_detection():
    clean()
    create_tmp_session("unreg-001")
    create_tmp_session("test-unreg")
    stdout, _, _ = run_hook("roster", {"session_id": "test-unreg", "cwd": TEST_CWD, "user_prompt": "x"})
    assert_test("roster:unreg:detected", "[cc]" in stdout, f"stdout: {stdout[:200]}")
    assert_test("roster:unreg:placeholder", "no metadata yet" in stdout or "just started" in stdout)


def test_roster_preserves_files():
    clean()
    create_tmp_session("preserve")
    run_hook("roster", {"session_id": "preserve", "cwd": TEST_CWD, "user_prompt": "a"})
    run_hook("touch", {"session_id": "preserve", "cwd": TEST_CWD,
                        "tool_input": {"file_path": f"{TEST_CWD}/foo.py"}})
    run_hook("roster", {"session_id": "preserve", "cwd": TEST_CWD, "user_prompt": "b"})
    team = read_team()
    member = next(m for m in team["members"] if m["agentId"] == "preserve")
    assert_test("roster:preserves_files", "foo.py" in member["files"])


def test_roster_long_prompt():
    clean()
    create_tmp_session("long")
    run_hook("roster", {"session_id": "long", "cwd": TEST_CWD, "user_prompt": "x" * 200})
    team = read_team()
    member = team["members"][0]
    assert_test("roster:truncation", len(member["task"]) <= 120)


def test_roster_unicode():
    clean()
    create_tmp_session("unicode")
    run_hook("roster", {"session_id": "unicode", "cwd": TEST_CWD, "user_prompt": "fix 日本語.py 🐛"})
    team = read_team()
    assert_test("roster:unicode", "日本語" in team["members"][0]["task"])


# ===========================================================================
# TOUCH
# ===========================================================================

def test_touch_basic():
    clean()
    create_tmp_session("touch-test")
    run_hook("roster", {"session_id": "touch-test", "cwd": TEST_CWD, "user_prompt": "work"})
    run_hook("touch", {"session_id": "touch-test", "cwd": TEST_CWD,
                        "tool_input": {"file_path": f"{TEST_CWD}/src/app.ts"}})
    team = read_team()
    member = next(m for m in team["members"] if m["agentId"] == "touch-test")
    assert_test("touch:basic:added", "src/app.ts" in member["files"])


def test_touch_relative():
    clean()
    create_tmp_session("touch-rel")
    run_hook("roster", {"session_id": "touch-rel", "cwd": "/Users/test/project", "user_prompt": "x"})
    run_hook("touch", {"session_id": "touch-rel", "cwd": "/Users/test/project",
                        "tool_input": {"file_path": "/Users/test/project/lib/utils.py"}})
    team = read_team("project") if False else None  # project is "project" here
    tf = TEAMS_DIR / "project" / "config.json"
    if tf.exists():
        team = json.loads(tf.read_text())
        member = team["members"][0]
        assert_test("touch:relative:converted", "lib/utils.py" in member["files"])
        assert_test("touch:relative:no_abs", not any(f.startswith("/") for f in member["files"]))
    else:
        assert_test("touch:relative:converted", False, "team file not found")
        assert_test("touch:relative:no_abs", False, "team file not found")


def test_touch_dedup():
    clean()
    create_tmp_session("touch-dd")
    run_hook("roster", {"session_id": "touch-dd", "cwd": TEST_CWD, "user_prompt": "x"})
    run_hook("touch", {"session_id": "touch-dd", "cwd": TEST_CWD, "tool_input": {"file_path": f"{TEST_CWD}/a.py"}})
    run_hook("touch", {"session_id": "touch-dd", "cwd": TEST_CWD, "tool_input": {"file_path": f"{TEST_CWD}/a.py"}})
    team = read_team()
    member = next(m for m in team["members"] if m["agentId"] == "touch-dd")
    assert_test("touch:dedup", member["files"].count("a.py") == 1)


def test_touch_max():
    clean()
    create_tmp_session("touch-max")
    run_hook("roster", {"session_id": "touch-max", "cwd": TEST_CWD, "user_prompt": "x"})
    for i in range(25):
        run_hook("touch", {"session_id": "touch-max", "cwd": TEST_CWD,
                            "tool_input": {"file_path": f"{TEST_CWD}/file{i}.py"}})
    team = read_team()
    member = next(m for m in team["members"] if m["agentId"] == "touch-max")
    assert_test("touch:max:capped", len(member["files"]) <= 20, f"got {len(member['files'])}")
    assert_test("touch:max:recent", "file24.py" in member["files"])


def test_touch_no_session():
    clean()
    stdout, stderr, code = run_hook("touch", {"session_id": "nope", "cwd": TEST_CWD,
                                                "tool_input": {"file_path": "/tmp/foo.py"}})
    assert_test("touch:no_session:exits_0", code == 0)


def test_touch_empty():
    clean()
    stdout, stderr, code = run_hook("touch", {})
    assert_test("touch:empty:exits_0", code == 0)


# ===========================================================================
# CLEANUP
# ===========================================================================

def test_cleanup_basic():
    clean()
    create_tmp_session("cleanup")
    run_hook("roster", {"session_id": "cleanup", "cwd": TEST_CWD, "user_prompt": "x"})
    team = read_team()
    assert_test("cleanup:before", len(team["members"]) == 1)
    run_hook("cleanup", {"session_id": "cleanup", "cwd": TEST_CWD})
    team = read_team()
    assert_test("cleanup:after", len(team["members"]) == 0)


def test_cleanup_nonexistent():
    clean()
    stdout, stderr, code = run_hook("cleanup", {"session_id": "nope", "cwd": TEST_CWD})
    assert_test("cleanup:nonexistent:exits_0", code == 0)


def test_cleanup_empty():
    clean()
    stdout, stderr, code = run_hook("cleanup", {})
    assert_test("cleanup:empty:exits_0", code == 0)


# ===========================================================================
# MAILBOX
# ===========================================================================

def test_mailbox_receive():
    clean()
    create_tmp_session("mail-rx")
    run_hook("roster", {"session_id": "mail-rx", "cwd": TEST_CWD, "user_prompt": "x"})
    write_inbox("mail-rx", [
        {"from": "sender", "text": "update your imports", "timestamp": "2026-03-31T05:30:00Z", "read": False, "summary": "import change"}
    ])
    stdout, _, _ = run_hook("roster", {"session_id": "mail-rx", "cwd": TEST_CWD, "user_prompt": "check"})
    assert_test("mailbox:rx:has_tag", "[cc]" in stdout)
    assert_test("mailbox:rx:has_text", "update your imports" in stdout)
    assert_test("mailbox:rx:has_from", "sender" in stdout)


def test_mailbox_mark_read():
    clean()
    create_tmp_session("mail-read")
    run_hook("roster", {"session_id": "mail-read", "cwd": TEST_CWD, "user_prompt": "x"})
    write_inbox("mail-read", [
        {"from": "a", "text": "hello", "timestamp": "now", "read": False}
    ])
    # First read — should show message
    stdout1, _, _ = run_hook("roster", {"session_id": "mail-read", "cwd": TEST_CWD, "user_prompt": "x"})
    # Second read — message should be marked read, not shown
    stdout2, _, _ = run_hook("roster", {"session_id": "mail-read", "cwd": TEST_CWD, "user_prompt": "x"})
    assert_test("mailbox:read:first", "hello" in stdout1)
    assert_test("mailbox:read:second", "hello" not in stdout2, f"got: {stdout2[:200]}")
    # Verify message still exists but is read
    inbox = read_inbox("mail-read")
    assert_test("mailbox:read:persisted", len(inbox) == 1 and inbox[0].get("read") is True)


# ===========================================================================
# SECURITY
# ===========================================================================

def test_security_path_traversal():
    clean()
    stdout, stderr, code = run_hook("roster", {
        "session_id": "../../../etc/passwd",
        "cwd": TEST_CWD, "user_prompt": "x",
    })
    assert_test("security:traversal:exits_0", code == 0)


# ===========================================================================
# ROBUSTNESS
# ===========================================================================

def test_robustness_corrupted_team():
    clean()
    create_tmp_session("robust")
    d = TEAMS_DIR / TEST_PROJECT
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text("{bad json!")
    stdout, stderr, code = run_hook("roster", {"session_id": "robust", "cwd": TEST_CWD, "user_prompt": "x"})
    assert_test("robustness:corrupted:exits_0", code == 0)
    team = read_team()
    assert_test("robustness:corrupted:recovered", team is not None and len(team.get("members", [])) > 0)


def test_robustness_missing_fields():
    clean()
    create_tmp_session("minimal")
    write_team({"name": TEST_PROJECT, "createdAt": 0, "members": [{"agentId": "minimal"}]})
    create_tmp_session("robust-min")
    stdout, stderr, code = run_hook("roster", {"session_id": "robust-min", "cwd": TEST_CWD, "user_prompt": "x"})
    assert_test("robustness:missing:exits_0", code == 0)


# ===========================================================================
# DISPATCHER
# ===========================================================================

def test_unknown_event():
    stdout, stderr, code = run_hook("bogus", {})
    assert_test("dispatcher:unknown:nonzero", code != 0)


def test_no_event():
    result = subprocess.run([sys.executable, str(HOOK_SCRIPT)], capture_output=True, text=True, timeout=5)
    assert_test("dispatcher:no_event:nonzero", result.returncode != 0)


def test_bad_json():
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), "roster"],
        input="not json {{{", capture_output=True, text=True, timeout=5,
    )
    assert_test("dispatcher:bad_json:exits_0", result.returncode == 0)


# ===========================================================================
# PERFORMANCE
# ===========================================================================

def test_performance():
    clean()
    for i in range(10):
        sid = f"perf-{i}"
        create_tmp_session(sid)
    write_team({
        "name": TEST_PROJECT, "createdAt": 0,
        "members": [
            {"agentId": f"perf-{i}", "name": f"{TEST_PROJECT}-{i}", "cwd": TEST_CWD,
             "branch": "main", "files": [f"f{j}.py" for j in range(5)],
             "task": f"task {i}", "isActive": True, "joinedAt": 0}
            for i in range(10)
        ]
    })
    create_tmp_session("perf-test")
    start = time.time()
    run_hook("roster", {"session_id": "perf-test", "cwd": TEST_CWD, "user_prompt": "x"})
    elapsed = time.time() - start
    assert_test("performance:under_2s", elapsed < 2.0, f"took {elapsed:.2f}s")


# ===========================================================================
# CONCURRENT
# ===========================================================================

def test_concurrent():
    clean()
    import concurrent.futures

    def run_one(i):
        sid = f"conc-{i}"
        create_tmp_session(sid)
        run_hook("roster", {"session_id": sid, "cwd": TEST_CWD, "user_prompt": f"task {i}"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(run_one, range(5)))

    team = read_team()
    assert_test("concurrent:all_registered", len(team["members"]) == 5,
                f"got {len(team['members'])}")


# ===========================================================================
# REAL SESSIONS
# ===========================================================================

def test_real_sessions():
    assert_test("real:tmp_exists", TMP_BASE.is_dir())
    live = {}
    if TMP_BASE.is_dir():
        for d in TMP_BASE.iterdir():
            if d.is_dir() and d.name.startswith("-"):
                sessions = [s.name for s in d.iterdir() if s.is_dir()]
                if sessions:
                    live[d.name] = sessions
    assert_test("real:found_some", len(live) > 0, f"found {len(live)} dirs")


# ===========================================================================
# Run
# ===========================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("cc v0.2 — team file + locking + XML roster + MCP")
    print("=" * 60)

    tests = [
        test_roster_basic,
        test_roster_no_session_id,
        test_roster_empty_payload,
        test_roster_shows_peers_xml,
        test_roster_file_conflict_xml,
        test_roster_cross_project_isolation,
        test_roster_name_autogeneration,
        test_roster_dead_session_pruned,
        test_roster_unregistered_detection,
        test_roster_preserves_files,
        test_roster_long_prompt,
        test_roster_unicode,
        test_touch_basic,
        test_touch_relative,
        test_touch_dedup,
        test_touch_max,
        test_touch_no_session,
        test_touch_empty,
        test_cleanup_basic,
        test_cleanup_nonexistent,
        test_cleanup_empty,
        test_mailbox_receive,
        test_mailbox_mark_read,
        test_security_path_traversal,
        test_robustness_corrupted_team,
        test_robustness_missing_fields,
        test_unknown_event,
        test_no_event,
        test_bad_json,
        test_performance,
        test_concurrent,
        test_real_sessions,
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

    clean()

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed ({passed + failed} total)")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(e)
    print("=" * 60)
    sys.exit(1 if failed > 0 else 0)
