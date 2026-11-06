"""The safety layer: hard timeout, whole-tree kill, no shell, capped output."""

import json
import os
import sys
import time

import pytest

from helpers import needs_node, pid_alive
from regexlab_mcp import engines as eng
from regexlab_mcp.sandbox import SandboxError, run_sandboxed

# A parent that starts a grandchild, reports both pids, then hangs.
HANG_TREE = """
import json, subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(json.dumps({"child": __import__("os").getpid(), "grandchild": g.pid}), flush=True)
time.sleep(120)
"""


def wait_dead(pid: int, seconds: float = 10.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def test_kill_proof_whole_process_tree_dies_at_the_timeout():
    t0 = time.perf_counter()
    res = run_sandboxed([sys.executable, "-c", HANG_TREE], "", timeout=2.0)
    wall = time.perf_counter() - t0
    assert res.timed_out
    assert wall < 15, f"returned after {wall:.1f}s; the 2s timeout was not enforced"
    pids = json.loads(res.stdout.strip().splitlines()[0])
    assert wait_dead(pids["child"]), "child process survived the kill"
    assert wait_dead(pids["grandchild"]), "grandchild survived: the tree was not killed"


def test_normal_run_returns_output_and_exit_code():
    res = run_sandboxed([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                        "hello", timeout=20)
    assert not res.timed_out and res.returncode == 0
    assert res.stdout.strip() == "HELLO"


def test_missing_executable_is_a_clear_error():
    with pytest.raises(SandboxError, match="could not start"):
        run_sandboxed(["definitely-not-a-real-binary-xyz"], "", timeout=5)


def test_python_engine_hang_is_killed_within_budget():
    t0 = time.perf_counter()
    run = eng.run_match("python", r"(a+)+$", "", ["a" * 60 + "!"], timeout=1.5)
    wall = time.perf_counter() - t0
    assert run.timed_out and run.cases == []
    assert wall < 12


@needs_node
def test_javascript_engine_hang_is_killed_within_budget():
    t0 = time.perf_counter()
    run = eng.run_match("javascript", r"(a+)+$", "", ["a" * 60 + "!"], timeout=1.5)
    wall = time.perf_counter() - t0
    assert run.timed_out
    assert wall < 12


def test_completed_cases_survive_a_later_hang():
    run = eng.run_match("python", r"(a+)+$", "", ["aaa", "a" * 60 + "!", "zzz"], timeout=1.5)
    assert run.timed_out
    assert len(run.cases) == 1  # the first case finished; the second hung; the third never ran
    assert [m.text for m in run.cases[0].matches] == ["aaa"]


def test_pattern_text_is_data_not_a_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nasty = "a; touch pwned.txt & echo $(touch pwned2.txt) `touch pwned3.txt` > pwned4.txt"
    eng.run_match("python", nasty, "", ["x"])
    assert os.listdir(tmp_path) == []


def test_python_runner_never_evaluates_the_pattern():
    # (?{...}) style code execution does not exist in re; a code-looking pattern is just a
    # compile error or a literal, never executed
    run = eng.run_match("python", r"__import__('os').system('echo hi')", "", ["x"])
    assert run.error is None and run.cases[0].matches == []


def test_limits_are_enforced():
    with pytest.raises(ValueError, match="limit"):
        eng.run_match("python", "a", "", ["x"] * 101)
    with pytest.raises(ValueError, match="limit"):
        eng.run_match("python", "a", "", ["x" * 150_000, "y" * 150_000])
    with pytest.raises(ValueError, match="limit"):
        eng.run_match("python", "a" * 6000, "", ["x"])
    with pytest.raises(ValueError, match="unknown flag"):
        eng.run_match("python", "a", "Q", ["x"])


def test_match_count_is_capped_and_marked():
    run = eng.run_match("python", "a", "", ["a" * 1000], max_matches=7)
    assert len(run.cases[0].matches) == 7 and run.cases[0].truncated


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is set by the runner on Linux only")
def test_linux_runner_caps_its_own_address_space():
    run = eng.run_match("python", "a", "", ["a"])
    assert run.mem_cap == 1 << 30


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object memory cap is Windows only")
def test_windows_job_object_caps_memory():
    res = run_sandboxed(
        [sys.executable, "-c", "b = bytearray(1500 * 1024 * 1024); print('alloc ok')"], "", timeout=30)
    assert "alloc ok" not in res.stdout and res.returncode != 0
