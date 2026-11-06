"""explain_regex: a token-by-token plain-English breakdown from our own parser."""

from __future__ import annotations

from . import rxparse as rx

_NAMES = {"\n": "a newline (LF)", "\r": "a carriage return (CR)", "\t": "a tab",
          "\f": "a form feed", "\v": "a vertical tab", "\x00": "a NUL character",
          "\b": "a backspace character", " ": "a space", "\x07": "a bell character"}


def _lit(ch: str) -> str:
    if ch in _NAMES:
        return _NAMES[ch]
    if ch.isprintable() and not ch.isspace():
        return f'the literal character "{ch}"'
    return f"the character U+{ord(ch):04X}"


class _Explainer:
    def __init__(self, parsed: rx.ParseResult) -> None:
        self.parsed = parsed
        self.p = parsed.pattern
        self.js = parsed.dialect == "javascript"
        self.lines: list[tuple[int, str, str]] = []
        self.flag_stack: list[set[str]] = [set(parsed.flags)]
        self.notes: list[str] = []
        self.unsupported: list[str] = []
        self.quant_modes: set[str] = set()

    @property
    def flags(self) -> set[str]:
        return self.flag_stack[-1]

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def emit(self, depth: int, src: str, desc: str) -> None:
        self.lines.append((depth, src, desc))

    # -- leaf descriptions ---------------------------------------------------

    def class_esc(self, letter: str) -> str:
        if self.js:
            table = {
                "d": "a digit 0-9 (ASCII only: JavaScript \\d never matches other scripts' digits)",
                "D": "any character that is not an ASCII digit 0-9",
                "w": "a word character [A-Za-z0-9_] (ASCII only in JavaScript)",
                "W": "any character that is not an ASCII word character",
                "s": "a whitespace character (space, tab, newlines, NBSP, BOM and Unicode spaces)",
                "S": "any non-whitespace character",
            }
        else:
            ascii_only = "a" in self.flags
            table = {
                "d": "a digit 0-9 (ASCII flag)" if ascii_only else
                "a decimal digit (any Unicode Nd digit, e.g. Arabic-Indic digits too, because "
                "Python str patterns are Unicode-aware)",
                "D": "any character that is not a decimal digit",
                "w": "a word character [A-Za-z0-9_] (ASCII flag)" if ascii_only else
                "a word character: any Unicode letter or digit, or underscore",
                "W": "any character that is not a word character",
                "s": "a whitespace character (Unicode-aware: includes NBSP and others)",
                "S": "any non-whitespace character",
            }
        return table[letter]

    def dot(self) -> str:
        if "s" in self.flags:
            return "any character, including newlines (dotall mode is on)"
        if self.js:
            return "any character except a line terminator (\\n, \\r, U+2028, U+2029)"
        return "any character except a newline (\\n)"

    def anchor(self, kind: str) -> str:
        multi = "m" in self.flags
        if kind == "bol":
            return ("the start of a line (start of the text, or right after a newline)"
                    if multi else "the start of the text")
        if kind == "eol":
            if multi:
                return "the end of a line (just before a newline, or the end of the text)"
            if self.js:
                return "the very end of the text (a trailing newline does NOT count)"
            self.note("In Python, `$` without the m flag also matches just before a trailing "
                      "newline: re.search('^abc$', 'abc\\n') succeeds. Use \\Z for the true end.")
            return ("the end of the text, or just before a newline that is the last character "
                    "(Python quirk; use \\Z for the true end)")
        if kind == "abs_start":
            return "the absolute start of the text (unaffected by the m flag)"
        if kind == "abs_end":
            return "the absolute end of the text (unaffected by the m flag)"
        if kind == "word_b":
            return "a word boundary (between a word character and a non-word character)"
        return "a position that is NOT a word boundary"

    def class_item(self, it: rx.Node) -> str:
        if isinstance(it, rx.Range):
            return f"{it.lo!r}-{it.hi!r}".replace("'", '"')
        if isinstance(it, rx.Char):
            return f'"{it.value}"' if it.value.isprintable() else _lit(it.value)
        if isinstance(it, rx.ClassEsc):
            self.note("\\" + it.letter + " inside a set: " + self.class_esc(it.letter))
            return {"d": "a digit", "D": "a non-digit", "w": "a word character",
                    "W": "a non-word character", "s": "whitespace",
                    "S": "non-whitespace"}[it.letter]
        if isinstance(it, rx.Prop):
            return f"Unicode property {it.name}" + (" (negated)" if it.negated else "")
        if isinstance(it, rx.Posix):
            return f"POSIX class [:{it.name}:]"
        return "?"

    def leaf(self, node: rx.Node) -> str | None:
        if isinstance(node, rx.Char):
            return _lit(node.value)
        if isinstance(node, rx.Dot):
            return self.dot()
        if isinstance(node, rx.Anchor):
            return self.anchor(node.kind)
        if isinstance(node, rx.ClassEsc):
            return self.class_esc(node.letter)
        if isinstance(node, rx.Prop):
            return ("a character that does NOT have" if node.negated else "a character with") \
                + f" the Unicode property {node.name}"
        if isinstance(node, rx.CharClass):
            parts = ", ".join(self.class_item(i) for i in node.items) or "nothing"
            if node.negated:
                if not node.items:
                    return "any character at all (an empty negated set)"
                return f"any single character EXCEPT: {parts}"
            if not node.items:
                return "nothing (an empty set never matches)"
            return f"any one of: {parts}"
        if isinstance(node, rx.Backref):
            if isinstance(node.ref, int):
                return f"the same text that capture group {node.ref} matched (a backreference)"
            return f'the same text that the group named "{node.ref}" matched (a backreference)'
        if isinstance(node, rx.Unsupported):
            self.unsupported.append(self.p[node.start:node.end])
            return f"NOT EXPLAINED - {node.reason}"
        if isinstance(node, rx.Comment):
            return f"a comment, ignored by the engine: {node.text.strip()!r}"
        if isinstance(node, rx.Flags):
            return self.flags_text(node.on, node.off, scoped=False)
        return None

    def flags_text(self, on: str, off: str, scoped: bool) -> str:
        names = {"i": "ignore case", "m": "multiline (^ and $ match at every line)",
                 "s": "dotall (. matches newlines)", "x": "verbose (whitespace and # comments "
                 "in the pattern are ignored)", "a": "ASCII-only \\w \\d \\s \\b",
                 "u": "Unicode", "L": "locale-dependent"}
        parts = [names.get(f, f) for f in on] + [f"turn off {names.get(f, f)}" for f in off]
        where = "inside this group only" if scoped else "for the whole pattern"
        return "inline flags: " + ", ".join(parts) + f" ({where})"

    def quant_text(self, q: rx.Quant) -> str:
        if q.min == 0 and q.max is None:
            base = "zero or more times"
        elif q.min == 1 and q.max is None:
            base = "one or more times"
        elif q.min == 0 and q.max == 1:
            base = "optionally (zero or one time)"
        elif q.max is None:
            base = f"{q.min} or more times"
        elif q.min == q.max:
            base = f"exactly {q.min} time{'s' if q.min != 1 else ''}"
        else:
            base = f"between {q.min} and {q.max} times"
        mode = {"greedy": "greedy", "lazy": "lazy: as few as possible",
                "possessive": "possessive: never gives back"}[q.mode]
        self.quant_modes.add(q.mode)
        return f"{base} ({mode})"

    def group_open(self, g: rx.Group) -> str:
        if g.kind == "capture":
            return f"start of capture group {g.index}"
        if g.kind == "named":
            return f'start of capture group {g.index}, named "{g.name}"'
        if g.kind == "noncap":
            return "start of a non-capturing group (groups the pieces without saving the text)"
        if g.kind == "atomic":
            return "start of an atomic group (once it matches, the engine will not backtrack into it)"
        if g.kind == "lookahead":
            return "start of a positive lookahead: what follows must match, but is not consumed"
        if g.kind == "neg_lookahead":
            return "start of a negative lookahead: what follows must NOT match; nothing is consumed"
        if g.kind == "lookbehind":
            return "start of a positive lookbehind: what precedes must match; nothing is consumed"
        if g.kind == "neg_lookbehind":
            return "start of a negative lookbehind: what precedes must NOT match; nothing is consumed"
        return "start of a group with " + self.flags_text(g.flags_on, g.flags_off, scoped=True)

    # -- walking -------------------------------------------------------------

    def alt(self, node: rx.Alt, depth: int) -> None:
        for i, br in enumerate(node.branches):
            if i:
                self.emit(depth, "|", "OR - or else try this alternative "
                          "(alternatives are tried left to right; the first that lets the whole "
                          "pattern match wins)")
            self.seq(br, depth)

    def seq(self, node: rx.Seq, depth: int) -> None:
        if not node.items:
            self.emit(depth, "", "(empty: matches the empty string)")
        for it in node.items:
            self.item(it, depth)

    def item(self, node: rx.Node, depth: int) -> None:
        if isinstance(node, rx.Quant):
            child = node.child
            qtext = self.p[node.qstart:node.end]
            if isinstance(child, rx.Group):
                self.group(child, depth, closing_suffix=qtext, quant=node)
            else:
                src = self.p[child.start:node.end]
                qdesc = self.leaf(child) or "?"
                self.emit(depth, src, f"{qdesc}, {self.quant_text(node)}")
            return
        if isinstance(node, rx.Group):
            self.group(node, depth)
            return
        desc = self.leaf(node)
        if desc is None:
            self.emit(depth, self.p[node.start:node.end], "?")
        else:
            self.emit(depth, self.p[node.start:node.end], desc)

    def group(self, g: rx.Group, depth: int, closing_suffix: str = "",
              quant: rx.Quant | None = None) -> None:
        self.emit(depth, self.p[g.start:g.header_end], self.group_open(g))
        if g.kind == "flags":
            new = set(self.flags)
            new |= set(g.flags_on)
            new -= set(g.flags_off)
            self.flag_stack.append(new)
        self.alt(g.body, depth + 1)
        if g.kind == "flags":
            self.flag_stack.pop()
        close = ")" + closing_suffix
        if quant is not None:
            what = "the group above"
            self.emit(depth, close, f"end of group; repeat {what} {self.quant_text(quant)}")
        else:
            self.emit(depth, close, "end of group")

    def run(self) -> str:
        self.alt(self.parsed.root, 0)
        width = max((len("  " * d + s) for d, s, _ in self.lines), default=0)
        width = min(max(width, 8), 34)
        out = []
        for depth, src, desc in self.lines:
            left = "  " * depth + src
            if len(left) > width:
                out.append(f"  {left}\n  {' ' * width}  {desc}")
            else:
                out.append(f"  {left.ljust(width)}  {desc}")
        return "\n".join(out)


def explain(parsed: rx.ParseResult) -> str:
    ex = _Explainer(parsed)
    body = ex.run()
    head = f"Pattern ({parsed.dialect}): {parsed.pattern}"
    if parsed.flags:
        head += f"   flags: {parsed.flags}"
    lines = [head, "", body, ""]
    if parsed.group_count:
        groups = []
        for node in rx.walk(parsed.root):
            if isinstance(node, rx.Group) and node.index is not None:
                label = f"{node.index}"
                if node.name:
                    label += f' ("{node.name}")'
                groups.append(f"{label} = {parsed.pattern[node.start:node.end]}")
        lines.append("Capture groups: " + "; ".join(groups))
    else:
        lines.append("Capture groups: none")
    if parsed.issues:
        lines.append("")
        for i in parsed.issues:
            tag = "ERROR" if i.level == "error" else "WARNING"
            lines.append(f"{tag} ({parsed.dialect}) at {i.start}-{i.end} "
                         f"`{parsed.pattern[i.start:i.end]}`: {i.message}")
    if ex.unsupported:
        lines.append("")
        lines.append("Not explained: " + ", ".join(f"`{u}`" for u in ex.unsupported)
                     + " (valid in some engines, but this explainer does not model it; "
                       "run it with test_regex to see what it does).")
    legend = {"greedy": "greedy = takes as many as it can and gives back if the rest fails",
              "lazy": "lazy = takes as few as it can and takes more only if the rest fails",
              "possessive": "possessive = takes as many as it can and never gives any back"}
    if ex.quant_modes:
        lines.append("")
        lines.append("Quantifiers: " + "; ".join(legend[m] for m in ("greedy", "lazy", "possessive")
                                                  if m in ex.quant_modes) + ".")
    if ex.notes:
        lines.append("")
        lines.extend(f"Note: {n}" for n in ex.notes)
    return "\n".join(lines)
