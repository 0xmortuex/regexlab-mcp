"""suggest_tests: generate should-match / should-not-match strings and VERIFY them.

Candidates come from the parsed structure (alternatives, quantifier bounds, class
boundaries) and from mutating the matching ones. Every candidate is then run in the
real engine; only confirmed examples are offered as tests, and the mutations that
still matched are shown separately because they usually reveal something (an
unanchored pattern, a `$` that accepts a trailing newline, an overly broad class).
"""

from __future__ import annotations

from . import engines as eng
from . import rxparse as rx
from .charsets import Cx
from .report import show
from .samples import Unsatisfiable, boundary_examples, positive_examples

SHOW_EACH = 10


def _mutations(positives: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [("the empty string", "")]
    for p in positives[:6]:
        if not p:
            out.append(("junk text instead of a match", "zzz!"))
            continue
        n = len(p)
        for i in sorted({0, n // 2, n - 1}):
            out.append((f"{show(p, 30)} with the character at index {i} deleted", p[:i] + p[i + 1:]))
        i = n // 2
        sub = "!" if p[i] != "!" else "~"
        out.append((f"{show(p, 30)} with the character at index {i} replaced by {sub!r}",
                    p[:i] + sub + p[i + 1:]))
        out.append((f"{show(p, 30)} with a junk prefix", "!" + p))
        out.append((f"{show(p, 30)} with a junk suffix", p + "!"))
        out.append((f"{show(p, 30)} with a trailing newline", p + "\n"))
        if p.swapcase() != p:
            out.append((f"{show(p, 30)} with letter case swapped", p.swapcase()))
    return out


def _anchored(root: rx.Alt) -> tuple[bool, bool]:
    def start_ok(b: rx.Seq) -> bool:
        return bool(b.items) and isinstance(b.items[0], rx.Anchor) and b.items[0].kind in (
            "bol", "abs_start")

    def end_ok(b: rx.Seq) -> bool:
        return bool(b.items) and isinstance(b.items[-1], rx.Anchor) and b.items[-1].kind in (
            "eol", "abs_end")

    return (all(start_ok(b) for b in root.branches), all(end_ok(b) for b in root.branches))


def suggest(pattern: str, engine: str, flags: str, *, timeout_s: float,
            notes: list[str]) -> str:
    eng.check_pattern(pattern)
    flags = eng.normalize_flags(flags)
    parsed = rx.parse(pattern, engine, flags)
    cx = Cx(engine, parsed.flags)
    try:
        positives = positive_examples(parsed.root, cx)
    except Unsatisfiable as e:
        raise ValueError(
            f"cannot generate examples for this pattern: {e}. Use test_regex with your own "
            "subjects instead.") from e

    cands: list[tuple[str, str, bool]] = [("structural example", p, True) for p in positives]
    try:
        for label, text in boundary_examples(parsed.root, cx, pattern):
            cands.append((label, text, False))
    except Unsatisfiable:
        pass
    for label, text in _mutations(positives):
        cands.append((label, text, False))
    seen: set[str] = set()
    uniq: list[tuple[str, str, bool]] = []
    for label, text, expect in cands:
        if text in seen:
            continue
        seen.add(text)
        uniq.append((label, text, expect))
    uniq = uniq[:eng.MAX_CASES]
    run = eng.run_match(engine, pattern, flags, [t for _, t, _ in uniq], timeout=timeout_s,
                        max_matches=1)
    if run.error:
        raise ValueError(f"the pattern does not compile in {engine}: {run.error}")
    if run.timed_out:
        raise ValueError(
            f"running the generated examples timed out after {run.elapsed:.1f}s in {engine}; "
            "the pattern backtracks catastrophically (run check_backtracking).")

    good: list[tuple[str, str, str]] = []
    bad: list[tuple[str, str]] = []
    surprises: list[tuple[str, str, str]] = []
    missed = 0
    for (label, text, expect), res in zip(uniq, run.cases):
        matched = bool(res.matches)
        where = ""
        if matched:
            m = res.matches[0]
            where = f"matches {show(m.text, 40)} at [{m.start}:{m.end}]"
        if expect and matched:
            good.append((label, text, where))
        elif expect:
            missed += 1
        elif matched:
            surprises.append((label, text, where))
        else:
            bad.append((label, text))

    def spread(items: list[tuple[str, ...]], k: int) -> list[tuple[str, ...]]:
        if len(items) <= k:
            return items
        step = (len(items) - 1) / (k - 1)
        return [items[round(i * step)] for i in range(k)]

    starts, ends = _anchored(parsed.root)
    lines = [f"Pattern: {pattern}" + (f"   flags: {flags}" if flags else ""),
             f"Verified by running every example in {engine} {run.version} with search "
             "semantics (does the pattern match anywhere in the text?).", ""]
    if not (starts and ends):
        which = "start" if not starts else "end"
        if not starts and not ends:
            which = "start or end"
        lines.append(f"The pattern is not anchored at the {which}, so it can match inside longer "
                     "text. If you are validating a whole string, anchor it or use fullmatch.")
        lines.append("")
    lines.append(f"SHOULD MATCH ({len(good)} confirmed, showing {min(len(good), SHOW_EACH)}):")
    for label, text, where in spread([(a, b, c) for a, b, c in good], SHOW_EACH):
        lines.append(f"  {show(text, 70):<40} {where}")
    if not good:
        lines.append("  (none: the generator could not build a matching string; use test_regex)")
    lines.append("")
    lines.append(f"SHOULD NOT MATCH ({len(bad)} confirmed, showing {min(len(bad), SHOW_EACH)}):")
    for label, text in spread([(a, b) for a, b in bad], SHOW_EACH):
        lines.append(f"  {show(text, 50):<30} {label}")
    if not bad:
        lines.append("  (none: every mutation still matched, see below)")
    if surprises:
        lines.append("")
        lines.append(f"SURPRISES - expected NO match but the engine matched ({len(surprises)}):")
        for label, text, where in spread([(a, b, c) for a, b, c in surprises], SHOW_EACH):
            lines.append(f"  {show(text, 50):<30} {where}   [{label}]")
        lines.append("  These are not failures of the tool: check whether the pattern is really "
                     "meant to accept them (unanchored pattern, `$` allowing a trailing newline "
                     "in Python, or a class that is broader than intended).")
    if missed:
        lines.append("")
        lines.append(f"{missed} generated example(s) were expected to match but did not "
                     "(lookarounds, backreferences or flags the generator does not model); they "
                     "were dropped rather than reported as tests.")
    for n in notes:
        lines.append(f"Note: {n}")
    return "\n".join(lines)
