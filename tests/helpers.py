"""Shared test helpers: skip markers for optional engines, process liveness."""

import importlib.util
import os
import shutil
import sys

import pytest

HAS_NODE = shutil.which("node") is not None
HAS_REGEX = importlib.util.find_spec("regex") is not None

needs_node = pytest.mark.skipif(not HAS_NODE, reason="node is not installed (JavaScript engine)")
needs_regex = pytest.mark.skipif(not HAS_REGEX, reason="the 'regex' module is not installed")
needs_311 = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="possessive quantifiers / atomic groups need Python 3.11+"
)


def engines_available() -> list[str]:
    out = ["python"]
    if HAS_NODE:
        out.append("javascript")
    if HAS_REGEX:
        out.append("regex")
    return out


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is running (a zombie counts as dead)."""
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        k32.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return True  # no /proc (macOS): kill(pid, 0) succeeded, so it is alive
