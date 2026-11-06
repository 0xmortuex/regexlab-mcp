"""Shared helpers: run several engines at once and format their results."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from . import engines as eng


def run_parallel(engines: list[str], fn: Callable[[str], eng.EngineRun]) -> list[eng.EngineRun]:
    """Run fn(engine) for each engine concurrently; results keep the input order."""
    if len(engines) == 1:
        return [fn(engines[0])]
    with ThreadPoolExecutor(max_workers=len(engines)) as pool:
        return list(pool.map(fn, engines))


def show(s: str, limit: int = 80) -> str:
    """repr() with a length limit, so odd characters are visible."""
    r = repr(s)
    return r if len(r) <= limit else r[: limit - 3] + "..."


def engine_header(runs: list[eng.EngineRun]) -> str:
    parts = []
    for r in runs:
        parts.append(f"{r.engine} {r.version}".strip())
    return " | ".join(parts)


def fmt_match(m: eng.Match) -> str:
    out = f"[{m.start}:{m.end}] {show(m.text)}"
    extras = []
    for i, g in enumerate(m.groups, 1):
        extras.append(f"{i}=" + ("(unmatched)" if g is None else f"{show(g.text, 40)}@{g.start}:{g.end}"))
    if extras:
        out += "  groups: " + " ".join(extras)
    if m.named:
        out += "  named: " + " ".join(
            f"{k}=" + ("(unmatched)" if v is None else show(v, 40)) for k, v in m.named.items())
    return out


def run_problem(r: eng.EngineRun, n_cases: int | None = None) -> str | None:
    """A one-line description when a run did not complete, else None."""
    if r.error:
        return f"{r.engine}: {r.error_kind or 'error'} - {r.error}"
    if r.timed_out:
        where = ""
        if n_cases is not None:
            where = f" while working on case {len(r.cases) + 1} of {n_cases}"
        return (f"{r.engine}: TIMED OUT after {r.elapsed:.1f}s{where}; the child process was "
                "killed. This pattern/input combination backtracks catastrophically in this "
                "engine (see check_backtracking).")
    return None


def fmt_cases(pattern_runs: list[eng.EngineRun], subjects: list[str],
              max_show: int = 20) -> list[str]:
    """Per-case, side-by-side result lines; flags disagreement between engines."""
    lines: list[str] = []
    for i, subject in enumerate(subjects):
        lines.append(f"case {i + 1}: {show(subject, 100)}")
        keys: list[tuple[object, ...] | None] = []
        for r in pattern_runs:
            if r.error:
                lines.append(f"  {r.engine}: ERROR ({r.error_kind}): {r.error}")
                keys.append(None)
                continue
            if i >= len(r.cases):
                lines.append(f"  {r.engine}: (not run - timed out earlier)" if r.timed_out else
                             f"  {r.engine}: (no result)")
                keys.append(None)
                continue
            cr = r.cases[i]
            if not cr.matches:
                lines.append(f"  {r.engine}: no match")
            else:
                lines.append(f"  {r.engine}: {len(cr.matches)} match{'es' if len(cr.matches) != 1 else ''}"
                             + (" (list truncated)" if cr.truncated else ""))
                for j, m in enumerate(cr.matches[:max_show], 1):
                    lines.append(f"    #{j} {fmt_match(m)}")
                if len(cr.matches) > max_show:
                    lines.append(f"    ... {len(cr.matches) - max_show} more")
            keys.append(tuple(m.key() for m in cr.matches))
        usable = [k for k in keys if k is not None]
        if len(usable) >= 2:
            if all(k == usable[0] for k in usable):
                errored = len(usable) < len(keys)
                lines.append("  => the engines that ran agree" if errored else "  => engines agree")
            else:
                lines.append("  => ENGINES DIFFER on this case (see compare_engines for why)")
    return lines
