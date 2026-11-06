"""replace_preview: show every substitution, using each engine's own template syntax."""

from __future__ import annotations

import re

from . import engines as eng
from .report import run_parallel, run_problem, show

_JS_TOKEN = re.compile(r"\$(?:\d{1,2}|&|`|'|\$|<[^>]*>)")
_PY_TOKEN = re.compile(r"\\(?:\d{1,3}|g<[^>]*>)")


def template_warnings(replacement: str, engines: list[str]) -> list[str]:
    out = []
    py_like = [e for e in engines if e in ("python", "regex")]
    if py_like and _JS_TOKEN.search(replacement):
        found = ", ".join(sorted({m.group(0) for m in _JS_TOKEN.finditer(replacement)}))
        out.append(f"the replacement contains JavaScript-style tokens ({found}); in "
                   f"{'/'.join(py_like)} `$` is plain text, so these are NOT substituted. "
                   "Python uses \\1, \\g<1> or \\g<name>.")
    if "javascript" in engines and _PY_TOKEN.search(replacement):
        found = ", ".join(sorted({m.group(0) for m in _PY_TOKEN.finditer(replacement)}))
        out.append(f"the replacement contains Python-style tokens ({found}); in JavaScript a "
                   "backslash is plain text, so these are NOT substituted. JavaScript uses $1, "
                   "$<name>, $& (whole match), $` (before), $' (after) and $$ (a dollar).")
    return out


def preview(pattern: str, replacement: str, subject: str, flags: str, engines: list[str], *,
            timeout_s: float, max_matches: int, notes: list[str]) -> str:
    flags = eng.normalize_flags(flags)
    runs = run_parallel(engines, lambda e: eng.run_sub(
        e, pattern, flags, replacement, subject, timeout=timeout_s, max_matches=max_matches))
    lines = [f"Pattern:     {pattern}" + (f"   flags: {flags}" if flags else ""),
             f"Replacement: {show(replacement, 120)}",
             f"Subject:     {show(subject, 120)}",
             "Engines: " + " | ".join(f"{r.engine} {r.version}".strip() for r in runs),
             "Spans are [start:end) in Unicode code points.", ""]
    for w in template_warnings(replacement, engines):
        lines.append(f"WARNING: {w}")
    if lines[-1].startswith("WARNING"):
        lines.append("")
    results: dict[str, str] = {}
    for r in runs:
        prob = run_problem(r)
        if prob:
            lines.append(prob)
            continue
        assert r.result_text is not None
        results[r.engine] = r.result_text
        n = len(r.substitutions)
        lines.append(f"{r.engine}: {n} substitution{'s' if n != 1 else ''}"
                     + (" (list truncated)" if r.sub_truncated else ""))
        for i, s in enumerate(r.substitutions, 1):
            lines.append(f"  #{i} [{s.start}:{s.end}] {show(s.text, 50)}  ->  {show(s.replacement, 60)}")
        lines.append(f"  result: {show(r.result_text, 300)}")
    if len(results) >= 2:
        lines.append("")
        if len(set(results.values())) == 1:
            lines.append("=> every engine produced the same result.")
        else:
            lines.append("=> RESULTS DIFFER between engines:")
            for e, v in results.items():
                lines.append(f"   {e}: {show(v, 200)}")
    for note in notes:
        lines.append(f"Note: {note}")
    return "\n".join(lines)
