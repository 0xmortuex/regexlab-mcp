"""Engine registry and the parent side of the child-process protocol."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sandbox import RunResult, SandboxError, run_sandboxed

ALL_ENGINES = ("python", "javascript", "regex")
ALIASES = {
    "python": "python", "py": "python", "re": "python",
    "javascript": "javascript", "js": "javascript", "node": "javascript",
    "regex": "regex",
}
LABELS = {"python": "Python re", "javascript": "JavaScript RegExp (node/V8)",
          "regex": "Python regex module"}

MAX_PATTERN = 5_000
MAX_CASES = 100
MAX_TOTAL_CHARS = 200_000
MAX_MATCHES = 500
DEFAULT_TIMEOUT = 5.0
MAX_TIMEOUT = 30.0
VALID_FLAGS = "gimsxuvyad"

_HERE = Path(__file__).parent
_RUNNER_PY = _HERE / "_runner_py.py"
_RUNNER_JS = _HERE / "_runner.js"


class EngineUnavailable(ValueError):
    pass


def node_path() -> str | None:
    return shutil.which("node")


def regex_module_available() -> bool:
    return importlib.util.find_spec("regex") is not None


def availability() -> dict[str, str | None]:
    """engine -> None when usable, else the reason it is not."""
    return {
        "python": None,
        "javascript": None if node_path() else
        "node was not found on PATH (install Node.js to enable the JavaScript engine)",
        "regex": None if regex_module_available() else
        "the third-party 'regex' module is not installed (pip install regex, or "
        "install regexlab-mcp[regex])",
    }


def pick_engines(requested: list[str] | str | None) -> tuple[list[str], list[str]]:
    """Resolve a user's engine choice to (engines to run, notes about skipped ones)."""
    avail = availability()
    if requested is None or requested in ("auto", "all") or requested == ["auto"] \
            or requested == ["all"]:
        engines = [e for e in ALL_ENGINES if avail[e] is None]
        notes = [f"{e}: skipped - {avail[e]}" for e in ALL_ENGINES if avail[e] is not None]
        return engines, notes
    names = [requested] if isinstance(requested, str) else list(requested)
    out: list[str] = []
    for n in names:
        key = ALIASES.get(n.strip().lower())
        if key is None:
            raise ValueError(
                f"unknown engine {n!r}; use python, javascript (or js/node), regex, or 'all'")
        if avail[key] is not None:
            raise EngineUnavailable(f"engine {key} is unavailable: {avail[key]}")
        if key not in out:
            out.append(key)
    if not out:
        raise ValueError("no engines requested")
    return out, []


def normalize_flags(flags: str) -> str:
    seen: list[str] = []
    for f in flags:
        if f not in VALID_FLAGS:
            raise ValueError(
                f"unknown flag {f!r}; flags are letters from {VALID_FLAGS!r}: "
                "i ignore case, m multiline, s dot matches newline, x verbose (Python only), "
                "a ASCII-only classes (Python only), u unicode, v unicodeSets (JS only), "
                "y sticky (JS only); g and d are accepted and ignored")
        if f not in seen:
            seen.append(f)
    return "".join(seen)


def check_pattern(pattern: str) -> None:
    if len(pattern) > MAX_PATTERN:
        raise ValueError(f"pattern is {len(pattern)} characters; the limit is {MAX_PATTERN}")


def check_subjects(subjects: list[str]) -> None:
    if not subjects:
        raise ValueError("no subject text given")
    if len(subjects) > MAX_CASES:
        raise ValueError(f"{len(subjects)} cases given; the limit is {MAX_CASES}")
    total = sum(len(s) for s in subjects)
    if total > MAX_TOTAL_CHARS:
        raise ValueError(f"subjects total {total} characters; the limit is {MAX_TOTAL_CHARS}")


def check_timeout(timeout_s: float) -> float:
    if not 0.2 <= timeout_s <= MAX_TIMEOUT:
        raise ValueError(f"timeout_s must be between 0.2 and {MAX_TIMEOUT}")
    return timeout_s


# -- result types -------------------------------------------------------------


@dataclass
class GroupSpan:
    start: int
    end: int
    text: str


@dataclass
class Match:
    start: int
    end: int
    text: str
    groups: list[GroupSpan | None]
    named: dict[str, str | None]

    def key(self) -> tuple[Any, ...]:
        """Comparable form used to decide whether two engines agree."""
        return (
            self.start, self.end, self.text,
            tuple(None if g is None else (g.start, g.end, g.text) for g in self.groups),
            tuple(sorted(self.named.items())),
        )


@dataclass
class CaseResult:
    matches: list[Match]
    truncated: bool


@dataclass
class Substitution:
    start: int
    end: int
    text: str
    replacement: str


@dataclass
class ProbePoint:
    group: str
    n: int
    length: int
    ms: float
    matched: bool


@dataclass
class EngineRun:
    engine: str
    version: str = ""
    error: str | None = None
    error_kind: str | None = None
    timed_out: bool = False
    elapsed: float = 0.0
    cases: list[CaseResult] = field(default_factory=list)
    substitutions: list[Substitution] = field(default_factory=list)
    sub_truncated: bool = False
    result_text: str | None = None
    result_truncated: bool = False
    probe: list[ProbePoint] = field(default_factory=list)
    group_count: int | None = None
    group_names: dict[str, int] = field(default_factory=dict)
    mem_cap: int | None = None  # address-space cap the Python runner set on itself (Linux)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out


def _argv(engine: str) -> list[str]:
    if engine == "javascript":
        node = node_path()
        if node is None:
            raise EngineUnavailable("node was not found on PATH")
        return [node, "--max-old-space-size=256", str(_RUNNER_JS)]
    return [sys.executable, "-I", str(_RUNNER_PY)]


def _group(d: dict[str, Any] | None) -> GroupSpan | None:
    return None if d is None else GroupSpan(d["start"], d["end"], d["text"])


def _match(d: dict[str, Any]) -> Match:
    return Match(d["start"], d["end"], d["text"], [_group(g) for g in d["groups"]], d["named"])


def _invoke(engine: str, request: dict[str, Any], timeout: float) -> EngineRun:
    request = dict(request, engine=engine)
    # ensure_ascii keeps the pipe pure ASCII, so no console code page can mangle it
    raw: RunResult = run_sandboxed(_argv(engine), json.dumps(request), timeout)
    run = EngineRun(engine=engine, timed_out=raw.timed_out, elapsed=raw.elapsed)
    done = False
    for line in raw.stdout.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            raise SandboxError(f"{engine} runner printed a non-JSON line: {line[:200]!r}") from e
        t = ev["t"]
        if t == "hello":
            run.version = str(ev.get("version", ""))
            run.mem_cap = ev.get("mem_cap")
        elif t == "compiled":
            run.group_count = ev.get("groups")
            run.group_names = ev.get("names", {})
        elif t == "error":
            run.error, run.error_kind = ev["message"], ev["kind"]
        elif t == "case":
            run.cases.append(CaseResult([_match(m) for m in ev["matches"]], ev["truncated"]))
        elif t == "sub":
            run.substitutions = [Substitution(m["start"], m["end"], m["text"], m["replacement"])
                                 for m in ev["matches"]]
            run.sub_truncated = ev["truncated"]
            run.result_text = ev["result"]
            run.result_truncated = ev["result_truncated"]
        elif t == "probe":
            run.probe.append(ProbePoint(ev["group"], ev["n"], ev["length"], ev["ms"], ev["matched"]))
        elif t == "done":
            done = True
    if not done and not raw.timed_out and run.error is None:
        tail = raw.stderr.strip()[-600:]
        raise SandboxError(
            f"the {engine} runner exited without finishing (exit code {raw.returncode}): "
            f"{tail or 'no stderr output'}")
    return run


def run_match(engine: str, pattern: str, flags: str, subjects: list[str], *,
              timeout: float = DEFAULT_TIMEOUT, max_matches: int = 100) -> EngineRun:
    check_pattern(pattern)
    check_subjects(subjects)
    max_matches = max(1, min(max_matches, MAX_MATCHES))
    return _invoke(engine, {"op": "match", "pattern": pattern, "flags": normalize_flags(flags),
                            "subjects": subjects, "max_matches": max_matches}, timeout)


def run_sub(engine: str, pattern: str, flags: str, replacement: str, subject: str, *,
            timeout: float = DEFAULT_TIMEOUT, max_matches: int = 100) -> EngineRun:
    check_pattern(pattern)
    check_subjects([subject])
    if len(replacement) > MAX_PATTERN:
        raise ValueError(f"replacement is {len(replacement)} characters; the limit is {MAX_PATTERN}")
    return _invoke(engine, {"op": "sub", "pattern": pattern, "flags": normalize_flags(flags),
                            "replacement": replacement, "subject": subject,
                            "max_matches": max(1, min(max_matches, MAX_MATCHES))}, timeout)


def run_probe(engine: str, pattern: str, flags: str, inputs: list[dict[str, Any]], *,
              timeout: float, stop_ms: float = 400.0) -> EngineRun:
    check_pattern(pattern)
    for item in inputs:
        size = len(item["prefix"]) + len(item["unit"]) * item["n"] + len(item["suffix"])
        if size > MAX_TOTAL_CHARS:
            raise ValueError("probe input too large")
    return _invoke(engine, {"op": "probe", "pattern": pattern, "flags": normalize_flags(flags),
                            "inputs": inputs, "stop_ms": stop_ms}, timeout)
