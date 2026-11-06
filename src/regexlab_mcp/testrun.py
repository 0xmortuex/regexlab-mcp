"""test_regex: run a pattern against subjects in each engine, side by side."""

from __future__ import annotations

from . import engines as eng
from .report import fmt_cases, run_parallel, run_problem


def test(pattern: str, subjects: list[str], flags: str, engines: list[str], *,
         timeout_s: float, max_matches: int, notes: list[str]) -> str:
    flags = eng.normalize_flags(flags)
    runs = run_parallel(engines, lambda e: eng.run_match(
        e, pattern, flags, subjects, timeout=timeout_s, max_matches=max_matches))
    lines = [f"Pattern: {pattern}" + (f"   flags: {flags}" if flags else ""),
             "Engines: " + " | ".join(f"{r.engine} {r.version}".strip() for r in runs),
             "Spans are [start:end) in Unicode code points, 0-based. Every match is listed "
             "(global search).", ""]
    lines.extend(fmt_cases(runs, subjects))
    problems = [p for r in runs if (p := run_problem(r, len(subjects))) and not r.error]
    if problems:
        lines.append("")
        lines.extend(problems)
    for n in notes:
        lines.append(f"Note: {n}")
    return "\n".join(lines)
