"""Run an untrusted workload in a child process with a hard timeout and a tree kill.

Regex engines can hang for hours on a bad pattern and cannot be interrupted from
inside the same process, so every pattern runs in its own child. No shell is ever
used; the argument vector is fixed by this package and user text only travels as
JSON on the child's stdin.

Limits:
- wall clock: hard timeout, then the whole process tree is killed (POSIX: the
  process group; Windows: the job object, with taskkill /T as a second line)
- memory: 1 GiB address space inside the Python runner on Linux (see
  _runner_py.py); a Job Object limit on Windows. macOS has no enforceable
  address-space limit, so there only the timeout protects you.
- output: captured output is truncated to MAX_OUTPUT bytes
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

MAX_OUTPUT = 4_000_000
MEMORY_LIMIT = 1 << 30


class SandboxError(RuntimeError):
    pass


@dataclass
class RunResult:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    elapsed: float
    pid: int


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_LIMIT_PROCESS_MEMORY = 0x100
    _JOB_LIMIT_KILL_ON_CLOSE = 0x2000
    _PROCESS_SET_QUOTA_TERMINATE = 0x0100 | 0x0001

    def _make_job(pid: int) -> int | None:
        """Put the child in a Job Object with a memory cap; None if that is not possible."""
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_LIMIT_PROCESS_MEMORY | _JOB_LIMIT_KILL_ON_CLOSE)
        info.ProcessMemoryLimit = MEMORY_LIMIT
        ok = _k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        handle = _k32.OpenProcess(_PROCESS_SET_QUOTA_TERMINATE, False, pid)
        if ok and handle and _k32.AssignProcessToJobObject(job, handle):
            _k32.CloseHandle(handle)
            return int(job)
        if handle:
            _k32.CloseHandle(handle)
        _k32.CloseHandle(job)
        return None

    def _terminate_job(job: int) -> None:
        _k32.TerminateJobObject(job, 1)

    def _close_job(job: int) -> None:
        _k32.CloseHandle(job)  # KILL_ON_JOB_CLOSE also reaps any straggler


def kill_tree(proc: subprocess.Popen[bytes], job: int | None = None) -> None:
    """Kill the child and everything it spawned, then reap it. Raises if it survives."""
    if sys.platform == "win32":
        if job is not None:
            _terminate_job(job)
        # Second line of defence (and the only one if the job could not be created).
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, check=False,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired as e:
        raise SandboxError(f"could not kill process {proc.pid}") from e


def run_sandboxed(argv: list[str], stdin_text: str, timeout: float) -> RunResult:
    """Run argv with stdin_text; kill the whole tree if it is still running after `timeout` s."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    t0 = time.perf_counter()
    try:
        if sys.platform == "win32":
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        else:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, start_new_session=True,
            )
    except OSError as e:
        raise SandboxError(f"could not start {argv[0]!r}: {e}") from e
    job: int | None = None
    if sys.platform == "win32":
        job = _make_job(proc.pid)
        if job is None:
            print("regexlab-mcp: could not create a Windows Job Object; "
                  "memory cap unavailable for this run", file=sys.stderr)
    timed_out = False
    try:
        out, err = proc.communicate(stdin_text.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc, job)
        out, err = proc.communicate()
    finally:
        if job is not None:
            _close_job(job)
    return RunResult(
        stdout=out[:MAX_OUTPUT].decode("utf-8", errors="replace"),
        stderr=err[:MAX_OUTPUT].decode("utf-8", errors="replace"),
        returncode=None if timed_out else proc.returncode,
        timed_out=timed_out,
        elapsed=time.perf_counter() - t0,
        pid=proc.pid,
    )
