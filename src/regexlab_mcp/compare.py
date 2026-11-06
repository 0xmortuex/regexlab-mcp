"""compare_engines: run one pattern everywhere and explain why results differ."""

from __future__ import annotations

import re

from . import engines as eng
from . import rxparse as rx
from .report import fmt_match, run_parallel, run_problem, show

_DIALECT = {"python": "python", "javascript": "javascript", "regex": "regex"}
_DIGITS = {ord(str(d)): chr(0x660 + d) for d in range(10)}


def _features(root: rx.Node) -> dict[str, bool]:
    nodes = rx.walk(root)
    f = {
        "eol": False, "digit": False, "word": False, "dot": False, "lookbehind": False,
        "backref": False, "optional_capture": False, "range_quant": False,
    }
    for n in nodes:
        if isinstance(n, rx.Anchor) and n.kind == "eol":
            f["eol"] = True
        elif isinstance(n, rx.ClassEsc) and n.letter in "dD":
            f["digit"] = True
        elif isinstance(n, rx.ClassEsc) and n.letter in "wWsS":
            f["word"] = True
        elif isinstance(n, rx.Anchor) and n.kind in ("word_b", "not_word_b"):
            f["word"] = True
        elif isinstance(n, rx.Dot):
            f["dot"] = True
        elif isinstance(n, rx.Group) and n.kind in ("lookbehind", "neg_lookbehind"):
            f["lookbehind"] = True
        elif isinstance(n, rx.Backref):
            f["backref"] = True
        elif isinstance(n, rx.Quant) and isinstance(n.child, rx.Group) and n.min == 0 \
                and n.child.index is not None:
            f["optional_capture"] = True
    return f


def _variants(pattern: str, flags: str, subject: str, feats: dict[str, bool]) -> list[tuple[str, str]]:
    out = [("your subject", subject)]
    if feats["eol"]:
        out.append(("your subject + a trailing newline", subject + "\n"))
    if feats["digit"]:
        if any(c.isdigit() for c in subject):
            out.append(("ASCII digits replaced by Arabic-Indic digits (U+0660-0669)",
                        subject.translate(_DIGITS)))
        else:
            out.append(("your subject + two Arabic-Indic digits", subject + " ٣٤"))
    if feats["word"]:
        out.append(("your subject + a non-ASCII word", subject + " café"))
    if feats["dot"]:
        out.append(("your subject + an emoji (one code point, two UTF-16 units)",
                    subject + " \U0001F600"))
    if "i" in flags:
        out.append(("your subject + Turkish/Unicode case pairs",
                    subject + " İ ı K ſ"))
    return out


def _rules(feats: dict[str, bool], flags: str, pattern: str) -> list[str]:
    notes = []
    if feats["eol"] and "m" not in flags:
        notes.append("`$` (no m flag): Python and the regex module also match just BEFORE a "
                     "trailing newline; JavaScript matches only at the very end. "
                     "Use \\Z in Python or a lookahead in JS if you need the exact end.")
    if feats["digit"]:
        notes.append("`\\d`: Python str patterns match any Unicode decimal digit (Arabic-Indic, "
                     "Devanagari, ...); JavaScript `\\d` is always [0-9]. Validate numbers with "
                     "[0-9] if you mean ASCII.")
    if feats["word"]:
        notes.append("`\\w \\s \\b`: Python is Unicode-aware by default ('café' is one word); "
                     "JavaScript `\\w` and `\\b` are ASCII-only, and JS `\\s` has its own "
                     "whitespace list (includes U+FEFF).")
    if feats["dot"]:
        notes.append("`.`: Python excludes only \\n; JavaScript excludes \\n \\r U+2028 U+2029. "
                     "Without the u flag, JavaScript's `.` matches a single UTF-16 unit, so an "
                     "emoji is two characters; Python counts it as one.")
    if feats["lookbehind"]:
        notes.append("Lookbehind: JavaScript allows variable width; Python `re` requires a "
                     "fixed width (the regex module allows variable width).")
    if feats["backref"] and feats["optional_capture"]:
        notes.append("A backreference to an optional group that did not participate: JavaScript "
                     "treats it as matching the empty string; Python fails the match.")
    if re.search(r"\{,\d+\}", pattern):
        notes.append("`{,n}`: Python reads it as {0,n}; JavaScript treats it as literal text.")
    if "i" in flags:
        notes.append("Case-insensitive matching outside ASCII differs: Python folds Unicode case "
                     "by default (e.g. 'k' matches the Kelvin sign U+212A), JavaScript only does "
                     "so with the u flag, and neither makes Turkish dotless/dotted i equal to "
                     "ASCII i/I the way a Turkish locale would.")
    return notes


def compare(pattern: str, subject: str, flags: str, engines: list[str], *, timeout_s: float,
            notes: list[str]) -> str:
    eng.check_pattern(pattern)
    flags = eng.normalize_flags(flags)
    # Syntax-level view: parse in each engine's dialect.
    parsed: dict[str, rx.ParseResult | str] = {}
    for e in engines:
        try:
            parsed[e] = rx.parse(pattern, _DIALECT[e], flags)
        except rx.ParseError as err:
            parsed[e] = str(err)
    ok_trees = [p for p in parsed.values() if isinstance(p, rx.ParseResult)]
    feats = _features(ok_trees[0].root) if ok_trees else _features(rx.Alt(start=0, end=0, branches=[]))
    eff_flags = flags + (ok_trees[0].flags if ok_trees else "")  # includes inline (?i) etc.
    variants = _variants(pattern, eff_flags, subject, feats)
    subjects = [v for _, v in variants]
    eng.check_subjects(subjects)

    runs = run_parallel(engines, lambda e: eng.run_match(
        e, pattern, flags, subjects, timeout=timeout_s, max_matches=50))

    lines = [f"Pattern: {pattern}" + (f"   flags: {flags}" if flags else ""),
             "Engines: " + " | ".join(f"{r.engine} {r.version}".strip() for r in runs), ""]

    lines.append("== Does it compile? ==")
    for r in runs:
        if r.error:
            lines.append(f"  {r.engine}: NO ({r.error_kind}) - {r.error}")
        elif r.timed_out:
            lines.append(f"  {r.engine}: TIMED OUT after {r.elapsed:.1f}s (killed)")
        else:
            lines.append(f"  {r.engine}: yes")
    syntax_lines: list[str] = []
    for e, p in parsed.items():
        if isinstance(p, str):
            syntax_lines.append(f"  {e}: invalid syntax in this dialect: {p}")
        else:
            seen: set[str] = set()
            for i in p.issues:
                msg = f"  {e}: {i.level.upper()} `{pattern[i.start:i.end]}` - {i.message}"
                if msg not in seen:
                    seen.add(msg)
                    syntax_lines.append(msg)
    if syntax_lines:
        lines.append("")
        lines.append("== Syntax differences found by parsing the pattern per dialect ==")
        lines.extend(syntax_lines)
        lines.append("  (convert_pattern can translate between dialects.)")

    lines.append("")
    lines.append("== Behaviour: same pattern, same text, every engine ==")
    usable = [r for r in runs if r.ok]
    differs_any = False
    for vi, (label, text) in enumerate(variants):
        keys = {r.engine: tuple(m.key() for m in r.cases[vi].matches) for r in usable
                if vi < len(r.cases)}
        agree = len(set(keys.values())) <= 1
        differs_any = differs_any or not agree
        if len(usable) < 2:
            tag = "only one engine ran"
        else:
            tag = "agree" if agree else "DIFFER"
        lines.append(f"  [{label}] {show(text, 70)} -> {tag}")
        if vi == 0 or not agree:
            for r in usable:
                if vi >= len(r.cases):
                    continue
                ms = r.cases[vi].matches
                if not ms:
                    lines.append(f"      {r.engine}: no match")
                for j, m in enumerate(ms[:6], 1):
                    lines.append(f"      {r.engine} #{j}: {fmt_match(m)}")
                if len(ms) > 6:
                    lines.append(f"      {r.engine}: ... {len(ms) - 6} more")
    for r in runs:
        prob = run_problem(r, len(variants))
        if prob and not r.error:
            lines.append(f"  {prob}")
    if len(usable) >= 2:
        lines.append("  Result: " + ("the engines DIFFER on at least one input above."
                                     if differs_any else
                                     "no difference on these inputs (that does not rule out "
                                     "differences on other inputs; see the notes below)."))

    rules = _rules(feats, eff_flags, pattern)
    if rules:
        lines.append("")
        lines.append("== Known semantic differences that apply to this pattern ==")
        lines.extend(f"  - {r}" for r in rules)
    for n in notes:
        lines.append("")
        lines.append(f"Note: {n}")
    return "\n".join(lines)
