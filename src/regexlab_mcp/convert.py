"""convert_pattern: best-effort translation between Python and JavaScript regex syntax.

Edits are made on the original text using the parser's spans, so everything we do
not understand stays exactly as written. Every non-trivial translation is listed,
everything we cannot translate is a warning, and the result is compiled in the target
engine (when available) so a wrong conversion is reported instead of trusted.
"""

from __future__ import annotations

from . import engines as eng
from . import rxparse as rx
from .report import show

_PY_LIKE = ("python", "regex")


def _canon(name: str) -> str:
    key = eng.ALIASES.get(name.strip().lower())
    if key is None:
        raise ValueError(f"unknown engine {name!r}; use python, javascript (or js), or regex")
    return key


class _Conv:
    def __init__(self, parsed: rx.ParseResult, src: str, dst: str, flags: str) -> None:
        self.parsed = parsed
        self.p = parsed.pattern
        self.src, self.dst = src, dst
        self.flags = list(flags)
        self.edits: list[tuple[int, int, str]] = []
        self.changes: list[str] = []
        self.warnings: list[str] = []

    def edit(self, start: int, end: int, text: str, why: str) -> None:
        self.edits.append((start, end, text))
        self.changes.append(f"`{self.p[start:end]}` -> `{text}`: {why}" if text else
                            f"removed `{self.p[start:end]}`: {why}")

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def set_flag(self, f: str) -> None:
        if f not in self.flags:
            self.flags.append(f)

    def drop_flag(self, f: str) -> None:
        self.flags = [x for x in self.flags if x != f]

    # -- python/regex -> javascript -------------------------------------------

    def to_js(self) -> None:
        multi = "m" in self.parsed.flags
        for f in list(self.flags):
            if f == "x":
                self.drop_flag("x")
                for s, e in self.parsed.ignored:
                    self.edit(s, e, "", "verbose-mode whitespace/comment (JavaScript has no x flag)")
            elif f == "a":
                self.drop_flag("a")
                self.warn("dropped the a (ASCII) flag: JavaScript \\w \\d \\b are already "
                          "ASCII-only, but \\s is wider than Python's ASCII \\s.")
            elif f == "L":
                self.drop_flag("L")
                self.warn("dropped the L (locale) flag: no JavaScript equivalent.")
            elif f == "u":
                pass
        features = set()
        for n in rx.walk(self.parsed.root):
            features.update(self.node_to_js(n, multi))
        if "unicode" in features:
            self.warn("Python's \\d \\w \\s \\b are Unicode-aware; JavaScript's are ASCII-only "
                      "(\\s differs too). Where it matters use \\p{Nd} / \\p{L} with the u flag, "
                      "or spell the class out. Not rewritten automatically.")
        if "dot" in features:
            self.warn("`.` in JavaScript also refuses \\r, U+2028 and U+2029 (Python only "
                      "refuses \\n), and without the u flag it matches half of an emoji.")
        if "lines" in features:
            self.warn("with the m flag JavaScript ^ and $ also break at \\r, U+2028 and U+2029.")

    def node_to_js(self, n: rx.Node, multi: bool) -> set[str]:
        out: set[str] = set()
        p = self.p
        if isinstance(n, rx.Group):
            if n.kind == "named" and p.startswith("(?P<", n.start):
                self.edit(n.start, n.start + 4, "(?<", "JavaScript spells named groups (?<name>...)")
            elif n.kind == "atomic":
                self.warn(f"atomic group `{p[n.start:n.header_end]}...)` has no JavaScript "
                          "equivalent (it can be emulated with (?=(...))\\N, which adds a capture "
                          "group); left unchanged and will not compile.")
            elif n.kind == "flags":
                self.warn(f"scoped inline flags `{p[n.start:n.header_end]}` need Node 23+ / "
                          "Chrome 125+; left unchanged.")
        elif isinstance(n, rx.Backref) and isinstance(n.ref, str):
            self.edit(n.start, n.end, f"\\k<{n.ref}>", "JavaScript named backreference")
        elif isinstance(n, rx.Anchor):
            if n.kind == "abs_start":
                self.edit(n.start, n.end, "(?<![\\s\\S])", "JavaScript has no \\A; this is exact")
            elif n.kind == "abs_end":
                self.edit(n.start, n.end, "(?![\\s\\S])", "JavaScript has no \\Z; this is exact")
            elif n.kind == "eol" and not multi:
                self.edit(n.start, n.end, "(?=\\n?(?![\\s\\S]))",
                          "Python's $ also matches before a final newline; this keeps that "
                          "(write a plain $ if you want JavaScript's stricter end)")
            elif n.kind in ("bol", "eol") and multi:
                out.add("lines")
            elif n.kind in ("word_b", "not_word_b"):
                out.add("unicode")
        elif isinstance(n, rx.ClassEsc):
            out.add("unicode")
        elif isinstance(n, rx.Dot):
            out.add("dot")
        elif isinstance(n, rx.Flags):
            for f in n.on:
                if f in "ims":
                    self.set_flag(f)
                elif f == "x":
                    self.warn("global (?x) inline flag cannot be translated; remove whitespace "
                              "and comments from the pattern by hand.")
                else:
                    self.warn(f"global inline flag {f} has no JavaScript equivalent.")
            self.edit(n.start, n.end, "", f"moved the inline flags ({n.on}) to the flags string")
        elif isinstance(n, rx.Comment):
            self.edit(n.start, n.end, "", "JavaScript has no (?#...) comments")
        elif isinstance(n, rx.Quant):
            if p.startswith("{,", n.qstart):
                self.edit(n.qstart, n.qstart + 2, "{0,", "Python reads {,n} as {0,n}; JavaScript "
                          "would treat it as literal text")
            if n.mode == "possessive":
                self.warn(f"possessive quantifier `{p[n.qstart:n.end]}` has no JavaScript "
                          "equivalent; left unchanged and will not compile.")
        elif isinstance(n, rx.Char) and n.escaped and p[n.start:n.start + 2] == "\\U":
            if ord(n.value) <= 0xFFFF:
                self.edit(n.start, n.end, f"\\u{ord(n.value):04X}", "JavaScript has no \\U")
            else:
                self.set_flag("u")
                self.edit(n.start, n.end, f"\\u{{{ord(n.value):X}}}", "JavaScript \\u{...} (needs the u flag)")
                self.warn("added the u flag for a \\u{...} escape; the u flag makes the engine "
                          "stricter about escapes and changes how `.` and classes see emoji.")
        elif isinstance(n, rx.Posix):
            self.warn(f"POSIX class [:{n.name}:] has no JavaScript equivalent.")
        elif isinstance(n, rx.Unsupported):
            self.warn(f"`{p[n.start:n.end]}` cannot be translated ({n.reason}).")
        return out

    # -- javascript -> python/regex ---------------------------------------------

    def to_py(self) -> None:
        for f in list(self.flags):
            if f == "g":
                self.drop_flag("g")
                self.changes.append("dropped flag g: Python has no global flag; use "
                                    "finditer/findall/sub, which are always global.")
            elif f == "d":
                self.drop_flag("d")
            elif f == "u":
                self.drop_flag("u")
                self.changes.append("dropped flag u: Python str patterns are Unicode already.")
            elif f in ("y", "v"):
                self.drop_flag(f)
                self.warn(f"dropped flag {f}: no Python equivalent (sticky -> use pattern.match "
                          "at a position; v -> use the regex module's set operations).")
        want_ascii = False
        dots = False
        for n in rx.walk(self.parsed.root):
            if isinstance(n, rx.ClassEsc) and n.letter in "dDwW":
                want_ascii = True
            elif isinstance(n, rx.Anchor) and n.kind in ("word_b", "not_word_b"):
                want_ascii = True
            elif isinstance(n, rx.Dot):
                dots = True
            self.node_to_py(n)
        if want_ascii:
            self.set_flag("a")
            self.warn("added the a (re.ASCII) flag so \\d \\w \\b are ASCII-only like "
                      "JavaScript's. Side effects: case-insensitive matching becomes ASCII-only "
                      "too, and \\s is narrower than JavaScript's \\s (no NBSP, BOM or Unicode "
                      "spaces).")
        if dots:
            self.warn("`.` in Python refuses only \\n; JavaScript also refuses \\r, U+2028 "
                      "and U+2029.")

    def node_to_py(self, n: rx.Node) -> None:
        p = self.p
        if isinstance(n, rx.Group):
            if n.kind == "named" and self.dst == "python":
                self.edit(n.start, n.start + 3, "(?P<", "Python spells named groups (?P<name>...)")
            elif n.kind in ("lookbehind", "neg_lookbehind") and self.dst == "python":
                lo, hi = rx.width(n.body)
                if hi is None or lo != hi:
                    self.warn(f"variable-width lookbehind `{p[n.start:n.end]}`: Python re requires "
                              "a fixed width (the regex module allows it); will not compile in re.")
        elif isinstance(n, rx.Backref) and isinstance(n.ref, str) and self.dst == "python":
            self.edit(n.start, n.end, f"(?P={n.ref})", "Python named backreference")
        elif isinstance(n, rx.Anchor) and n.kind == "eol" and "m" not in self.parsed.flags:
            self.edit(n.start, n.end, "\\Z", "JavaScript's $ is the true end of text; in Python "
                      "that is \\Z (a plain $ also matches before a final newline)")
        elif isinstance(n, rx.Char) and n.escaped and len(p[n.start:n.end]) == 2 \
                and p[n.start + 1] == n.value and n.value.isalpha() and n.value.isascii():
            self.edit(n.start, n.end, n.value, f"JavaScript treats \\{n.value} as a plain "
                      f"'{n.value}'; Python would reject or reinterpret it")
        elif isinstance(n, rx.Char) and n.escaped and p.startswith("\\u{", n.start):
            self.edit(n.start, n.end, f"\\U{ord(n.value):08X}", "Python spells code points \\UXXXXXXXX")
        elif isinstance(n, rx.CharClass) and not n.items:
            if n.negated:
                self.edit(n.start, n.end, "[\\s\\S]", "JavaScript [^] means any character")
            else:
                self.edit(n.start, n.end, "(?!)", "JavaScript [] never matches")
        elif isinstance(n, rx.Prop) and self.dst == "python":
            self.warn(f"`{p[n.start:n.end]}`: Python re has no \\p{{...}} property escapes "
                      "(the regex module does); left unchanged and will not compile in re.")
        elif isinstance(n, rx.Group) and n.kind == "flags":
            pass
        elif isinstance(n, rx.Unsupported):
            self.warn(f"`{p[n.start:n.end]}` cannot be translated ({n.reason}).")

    def result(self) -> str:
        spans = sorted(self.edits, key=lambda e: (e[0], e[1]), reverse=True)
        out = self.p
        last = len(self.p) + 1
        for s, e, text in spans:
            if e > last:
                raise AssertionError("overlapping conversion edits")  # parser/edit bug
            out = out[:s] + text + out[e:]
            last = s
        return out


def convert(pattern: str, from_engine: str, to_engine: str, flags: str, *,
            timeout_s: float) -> str:
    src, dst = _canon(from_engine), _canon(to_engine)
    eng.check_pattern(pattern)
    flags = eng.normalize_flags(flags)
    parsed = rx.parse(pattern, src, flags)
    c = _Conv(parsed, src, dst, flags)
    if src == dst:
        head = f"Source and target are both {src}: nothing to convert."
    elif src in _PY_LIKE and dst in _PY_LIKE:
        head = f"{src} -> {dst}: the syntaxes are compatible; pattern unchanged."
        if src == "regex":
            for n in rx.walk(parsed.root):
                if isinstance(n, (rx.Prop, rx.Posix, rx.Unsupported)):
                    c.warn(f"`{pattern[n.start:n.end]}` is regex-module syntax and will not "
                           "compile in Python re.")
        if src == "python" and dst == "regex":
            c.warn("the regex module differs from re in some corners (e.g. nested sets, "
                   "zero-width splits, full case folding with the f flag); results of the "
                   "same pattern are usually identical but not guaranteed.")
    elif src in _PY_LIKE and dst == "javascript":
        head = f"{src} -> javascript"
        c.to_js()
    else:
        head = f"javascript -> {dst}"
        c.to_py()

    new_pattern = c.result()
    new_flags = eng.normalize_flags("".join(c.flags))
    lines = [head, "", f"  pattern: {new_pattern}", f"  flags:   {new_flags or '(none)'}", ""]
    if c.changes:
        lines.append("Changes made:")
        lines.extend(f"  - {x}" for x in c.changes)
        lines.append("")
    if c.warnings:
        lines.append("WARNINGS (things I could not make equivalent):")
        lines.extend(f"  - {x}" for x in c.warnings)
        lines.append("")
    if not c.changes and not c.warnings:
        lines.append("No syntax differences found.")
        lines.append("")
    avail = eng.availability()
    if avail[dst] is not None:
        lines.append(f"Not verified: {avail[dst]}")
    else:
        run = eng.run_match(dst, new_pattern, new_flags, [""], timeout=timeout_s)
        if run.error:
            lines.append(f"VERIFY: the converted pattern does NOT compile in {dst}: {run.error}")
        elif run.timed_out:
            lines.append(f"VERIFY: compiling/running in {dst} timed out")
        else:
            lines.append(f"VERIFY: the converted pattern compiles in {dst} {run.version}.")
    lines.append("Compiling is not the same as equivalent: run both versions on real inputs with "
                 f"test_regex / compare_engines before relying on it. Original: {show(pattern, 100)}")
    return "\n".join(lines)
