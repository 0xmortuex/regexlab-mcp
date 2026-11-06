"""A parser for the common subset of Python and JavaScript regex syntax.

It builds a small syntax tree where every node remembers its [start, end) span in
the pattern text. The tree feeds the explainer, the static backtracking analysis,
the converter and the test generator.

Anything the parser does not understand is either a ParseError (the pattern is
malformed in that dialect) or an `Unsupported` node (valid in some engine but not
modelled here). It never guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DIALECTS = ("python", "javascript", "regex")


class ParseError(ValueError):
    def __init__(self, message: str, pos: int) -> None:
        super().__init__(f"{message} at position {pos}")
        self.reason = message
        self.pos = pos
        self.pattern = ""
        self.dialect = ""

    def detail(self) -> str:
        """Message plus the pattern with a caret under the offending position."""
        if not self.pattern:
            return str(self)
        return (f"invalid {self.dialect} pattern: {self}\n  {self.pattern}\n  "
                f"{' ' * self.pos}^")


@dataclass(kw_only=True, eq=False)
class Node:
    start: int
    end: int


@dataclass(kw_only=True, eq=False)
class Char(Node):
    value: str
    escaped: bool = False


@dataclass(kw_only=True, eq=False)
class Dot(Node):
    pass


@dataclass(kw_only=True, eq=False)
class Anchor(Node):
    # bol ^, eol $, abs_start \A, abs_end \Z or \z, word_b \b, not_word_b \B
    kind: str


@dataclass(kw_only=True, eq=False)
class ClassEsc(Node):
    letter: str  # d D w W s S


@dataclass(kw_only=True, eq=False)
class Prop(Node):
    name: str
    negated: bool


@dataclass(kw_only=True, eq=False)
class Posix(Node):
    name: str
    negated: bool


@dataclass(kw_only=True, eq=False)
class Range(Node):
    lo: str
    hi: str


@dataclass(kw_only=True, eq=False)
class CharClass(Node):
    negated: bool
    items: list[Node]


@dataclass(kw_only=True, eq=False)
class Backref(Node):
    ref: int | str


@dataclass(kw_only=True, eq=False)
class Flags(Node):
    on: str
    off: str


@dataclass(kw_only=True, eq=False)
class Comment(Node):
    text: str


@dataclass(kw_only=True, eq=False)
class Unsupported(Node):
    reason: str


@dataclass(kw_only=True, eq=False)
class Seq(Node):
    items: list[Node]


@dataclass(kw_only=True, eq=False)
class Alt(Node):
    branches: list[Seq]


@dataclass(kw_only=True, eq=False)
class Group(Node):
    # capture, named, noncap, atomic, lookahead, neg_lookahead, lookbehind,
    # neg_lookbehind, flags (scoped inline flags)
    kind: str
    body: Alt
    header_end: int
    index: int | None = None
    name: str | None = None
    flags_on: str = ""
    flags_off: str = ""


@dataclass(kw_only=True, eq=False)
class Quant(Node):
    child: Node
    min: int
    max: int | None  # None = unbounded
    mode: str  # greedy, lazy, possessive
    qstart: int


@dataclass
class Issue:
    start: int
    end: int
    level: str  # "error": the engine will reject it; "warn": works but differs
    message: str


@dataclass
class ParseResult:
    root: Alt
    pattern: str
    dialect: str
    flags: str
    issues: list[Issue] = field(default_factory=list)
    group_count: int = 0
    group_names: dict[str, int] = field(default_factory=dict)
    ignored: list[tuple[int, int]] = field(default_factory=list)


_QUANT_PY = re.compile(r"\{(\d*)(?:(,)(\d*))?\}")
_QUANT_JS = re.compile(r"\{(\d+)(?:(,)(\d*))?\}")
_PY_NAME = re.compile(r"[^\W\d]\w*")
_JS_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_HEX = "0123456789abcdefABCDEF"
_SIMPLE_ESC = {"n": "\n", "r": "\r", "t": "\t", "f": "\f", "v": "\v"}
_PY_BAD_LETTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_FLAG_LETTERS = "aiLmsux"


class _Parser:
    def __init__(self, pattern: str, dialect: str, flags: str) -> None:
        self.p = pattern
        self.i = 0
        self.dialect = dialect
        self.is_js = dialect == "javascript"
        self.flags = set(flags)
        self.verbose = ("x" in self.flags) and not self.is_js
        self.group_count = 0
        self.group_names: dict[str, int] = {}
        self.issues: list[Issue] = []
        self.ignored: list[tuple[int, int]] = []
        self.backrefs: list[Backref] = []
        self.unicode_mode = self.is_js and ("u" in self.flags or "v" in self.flags)

    # -- helpers -----------------------------------------------------------

    def issue(self, start: int, end: int, level: str, message: str) -> None:
        self.issues.append(Issue(start, end, level, message))

    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.p[j] if j < len(self.p) else ""

    def startswith(self, s: str) -> bool:
        return self.p.startswith(s, self.i)

    def skip_verbose(self) -> None:
        """In verbose mode whitespace and #-comments outside classes are ignored."""
        while self.verbose and self.i < len(self.p):
            c = self.p[self.i]
            if c.isspace():
                s = self.i
                while self.i < len(self.p) and self.p[self.i].isspace():
                    self.i += 1
                self.ignored.append((s, self.i))
            elif c == "#":
                s = self.i
                while self.i < len(self.p) and self.p[self.i] != "\n":
                    self.i += 1
                self.ignored.append((s, self.i))
            else:
                break

    # -- grammar -----------------------------------------------------------

    def parse(self) -> Alt:
        node = self.parse_alt()
        if self.i < len(self.p):
            raise ParseError("unbalanced parenthesis (unexpected ')')", self.i)
        for br in self.backrefs:
            if isinstance(br.ref, int) and br.ref > self.group_count:
                if self.is_js:
                    self.issue(
                        br.start, br.end, "warn",
                        f"\\{br.ref} refers to a group that does not exist; JavaScript "
                        "reads it as a legacy octal escape instead of a backreference",
                    )
                else:
                    raise ParseError(f"invalid group reference {br.ref}", br.start)
            elif isinstance(br.ref, str) and br.ref not in self.group_names:
                raise ParseError(f"unknown group name {br.ref!r}", br.start)
        return node

    def parse_alt(self) -> Alt:
        start = self.i
        branches = [self.parse_seq()]
        while self.peek() == "|":
            self.i += 1
            branches.append(self.parse_seq())
        return Alt(start=start, end=self.i, branches=branches)

    def parse_seq(self) -> Seq:
        start = self.i
        items: list[Node] = []
        while True:
            self.skip_verbose()
            if self.i >= len(self.p) or self.peek() in ("|", ")"):
                break
            atom = self.parse_atom()
            if atom is None:
                continue
            atom = self.parse_quantifiers(atom)
            items.append(atom)
        return Seq(start=start, end=self.i, items=items)

    def match_quantifier(self) -> tuple[int, int | None, int] | None:
        """Return (min, max, length of the quantifier text) at the cursor, or None."""
        c = self.peek()
        if c == "*":
            return 0, None, 1
        if c == "+":
            return 1, None, 1
        if c == "?":
            return 0, 1, 1
        if c == "{":
            m = (_QUANT_JS if self.is_js else _QUANT_PY).match(self.p, self.i)
            if m is None:
                return None
            lo, comma, hi = m.group(1), m.group(2), m.group(3)
            if not self.is_js and not lo and not hi and comma is None:
                return None  # "{}" is literal text
            mn = int(lo) if lo else 0
            if comma is None:
                mx: int | None = mn
            else:
                mx = int(hi) if hi else None
            return mn, mx, m.end() - self.i
        return None

    def parse_quantifiers(self, atom: Node) -> Node:
        self.skip_verbose()
        q = self.match_quantifier()
        if q is None:
            return atom
        if isinstance(atom, (Anchor, Flags, Comment)) or (
            isinstance(atom, Group) and atom.kind in ("lookbehind", "neg_lookbehind")
        ):
            raise ParseError("nothing to repeat", self.i)
        if isinstance(atom, Group) and atom.kind in ("lookahead", "neg_lookahead") and not self.is_js:
            raise ParseError("nothing to repeat (lookaheads cannot be quantified)", self.i)
        mn, mx, length = q
        qstart = self.i
        self.i += length
        if mx is not None and mn > mx:
            raise ParseError("min repeat greater than max repeat", qstart)
        mode = "greedy"
        if self.peek() == "?":
            mode = "lazy"
            self.i += 1
        elif self.peek() == "+":
            mode = "possessive"
            self.i += 1
            if self.is_js:
                self.issue(qstart, self.i, "error",
                           "possessive quantifiers are not supported in JavaScript "
                           "(SyntaxError: Nothing to repeat)")
            elif self.dialect == "python":
                self.issue(qstart, self.i, "warn",
                           "possessive quantifiers need Python 3.11 or newer")
        self.skip_verbose()
        if self.match_quantifier() is not None:
            raise ParseError("multiple repeat", self.i)
        return Quant(start=atom.start, end=self.i, child=atom, min=mn, max=mx,
                     mode=mode, qstart=qstart)

    def parse_atom(self) -> Node | None:
        c = self.peek()
        s = self.i
        if c == "(":
            return self.parse_group()
        if c == "[":
            return self.parse_class()
        if c == "\\":
            return self.parse_escape(in_class=False)
        if c == ".":
            self.i += 1
            return Dot(start=s, end=self.i)
        if c == "^":
            self.i += 1
            return Anchor(start=s, end=self.i, kind="bol")
        if c == "$":
            self.i += 1
            return Anchor(start=s, end=self.i, kind="eol")
        if c in ("*", "+", "?"):
            raise ParseError("nothing to repeat", s)
        if c == "{":
            if self.match_quantifier() is not None:
                raise ParseError("nothing to repeat", s)
            self.i += 1
            return Char(start=s, end=self.i, value="{")
        self.i += 1
        return Char(start=s, end=self.i, value=c)

    # -- escapes -----------------------------------------------------------

    def parse_escape(self, in_class: bool) -> Node:
        s = self.i
        self.i += 1  # backslash
        if self.i >= len(self.p):
            raise ParseError("bad escape (end of pattern)", s)
        c = self.p[self.i]
        self.i += 1

        def ch(value: str) -> Char:
            return Char(start=s, end=self.i, value=value, escaped=True)

        if c in "dDwWsS":
            return ClassEsc(start=s, end=self.i, letter=c)
        if c in _SIMPLE_ESC:
            return ch(_SIMPLE_ESC[c])
        if c == "b":
            if in_class:
                return ch("\b")
            return Anchor(start=s, end=self.i, kind="word_b")
        if c == "B":
            if in_class:
                if self.is_js:
                    return ch("B")
                raise ParseError("bad escape \\B", s)
            return Anchor(start=s, end=self.i, kind="not_word_b")
        if c in "AZz" and not in_class:
            if self.is_js:
                self.issue(s, self.i, "warn",
                           f"in JavaScript \\{c} is just a literal '{c}', not an anchor "
                           "(use ^ and $, or (?<![\\s\\S]) and (?![\\s\\S]))")
                return ch(c)
            if c == "A":
                return Anchor(start=s, end=self.i, kind="abs_start")
            if c == "Z":
                return Anchor(start=s, end=self.i, kind="abs_end")
            if self.dialect == "python":
                self.issue(s, self.i, "warn", "\\z needs Python 3.14 or newer")
            return Anchor(start=s, end=self.i, kind="abs_end")
        if c == "0":
            digits = ""
            while len(digits) < 2 and self.peek() in tuple("01234567") and self.peek():
                digits += self.peek()
                self.i += 1
            return ch(chr(int(digits, 8)) if digits else "\x00")
        if c in "123456789":
            if in_class:
                if self.is_js:
                    return ch(c)
                raise ParseError(f"bad escape \\{c} in character class", s)
            digits = c
            if self.is_js:
                while self.peek().isdigit():
                    digits += self.peek()
                    self.i += 1
            else:
                if c in "0123" and self.peek() in tuple("01234567") and self.peek() \
                        and self.peek(1) in tuple("01234567") and self.peek(1):
                    oct3 = c + self.peek() + self.peek(1)
                    self.i += 2
                    return ch(chr(int(oct3, 8)))
                if self.peek().isdigit() and self.peek():
                    digits += self.peek()
                    self.i += 1
            br = Backref(start=s, end=self.i, ref=int(digits))
            self.backrefs.append(br)
            return br
        if c == "x":
            hx = self.p[self.i:self.i + 2]
            if len(hx) == 2 and all(h in _HEX for h in hx):
                self.i += 2
                return ch(chr(int(hx, 16)))
            if self.is_js:
                return ch("x")
            raise ParseError("incomplete escape \\x", s)
        if c == "u":
            if self.is_js and self.peek() == "{" and self.unicode_mode:
                close = self.p.find("}", self.i)
                body = self.p[self.i + 1:close] if close != -1 else ""
                if close == -1 or not body or not all(h in _HEX for h in body):
                    raise ParseError("invalid Unicode escape \\u{...}", s)
                cp = int(body, 16)
                if cp > 0x10FFFF:
                    raise ParseError("Unicode escape out of range", s)
                self.i = close + 1
                return ch(chr(cp))
            hx = self.p[self.i:self.i + 4]
            if len(hx) == 4 and all(h in _HEX for h in hx):
                self.i += 4
                return ch(chr(int(hx, 16)))
            if self.is_js and not self.unicode_mode:
                return ch("u")
            raise ParseError("incomplete escape \\u", s)
        if c == "U":
            if self.is_js:
                return ch("U")
            hx = self.p[self.i:self.i + 8]
            if len(hx) == 8 and all(h in _HEX for h in hx) and int(hx, 16) <= 0x10FFFF:
                self.i += 8
                return ch(chr(int(hx, 16)))
            raise ParseError("incomplete escape \\U", s)
        if c == "N" and not self.is_js:
            close = self.p.find("}", self.i) if self.peek() == "{" else -1
            if close == -1:
                raise ParseError("missing {NAME} after \\N", s)
            self.i = close + 1
            return Unsupported(start=s, end=self.i,
                               reason="named Unicode character escapes (\\N{...}) are not looked up")
        if c in "pP" and (self.is_js or self.dialect == "regex"):
            if self.is_js and not self.unicode_mode:
                self.issue(s, self.i, "warn",
                           f"without the u (or v) flag \\{c} is just a literal '{c}' in JavaScript, "
                           "not a Unicode property escape")
                return ch(c)
            if self.peek() == "{":
                close = self.p.find("}", self.i)
                if close == -1:
                    raise ParseError(f"missing }} in \\{c}{{...}}", s)
                name = self.p[self.i + 1:close]
                self.i = close + 1
            else:
                name = self.peek()
                self.i += 1
            return Prop(start=s, end=self.i, name=name.lstrip("^"),
                        negated=(c == "P") != name.startswith("^"))
        if c == "k" and not in_class and (self.is_js or self.dialect == "regex"):
            if self.peek() == "<":
                close = self.p.find(">", self.i)
                if close == -1:
                    raise ParseError("missing > in \\k<name>", s)
                name = self.p[self.i + 1:close]
                self.i = close + 1
                br = Backref(start=s, end=self.i, ref=name)
                self.backrefs.append(br)
                return br
            if self.is_js and not self.unicode_mode and not self.group_names:
                return ch("k")
            raise ParseError("\\k must be followed by <name>", s)
        if c == "c" and self.is_js:
            if self.peek().isalpha() and self.peek().isascii():
                letter = self.peek()
                self.i += 1
                return ch(chr(ord(letter) % 32))
            # "\c" without a letter: the backslash is a literal and "c" is read next.
            self.i = s + 1
            return Char(start=s, end=s + 1, value="\\")
        if c in "GK" and not in_class and self.dialect == "regex":
            return Unsupported(start=s, end=self.i,
                               reason=f"\\{c} (regex-module extension) is not modelled")
        if self.is_js:
            return ch(c)
        if c in _PY_BAD_LETTERS:
            raise ParseError(f"bad escape \\{c}", s)
        return ch(c)

    # -- classes -----------------------------------------------------------

    def parse_class(self) -> Node:
        s = self.i
        self.i += 1  # [
        negated = False
        if self.peek() == "^":
            negated = True
            self.i += 1
        items: list[Node] = []
        first = True
        while True:
            if self.i >= len(self.p):
                raise ParseError("unterminated character set", s)
            c = self.p[self.i]
            if c == "]" and not (first and not self.is_js):
                self.i += 1
                break
            first = False
            item = self.parse_class_atom()
            if (
                isinstance(item, Char)
                and self.peek() == "-"
                and self.peek(1) not in ("]", "")
            ):
                save = self.i
                self.i += 1  # '-'
                hi = self.parse_class_atom()
                if isinstance(hi, Char):
                    if hi.value < item.value:
                        raise ParseError(
                            f"bad character range {self.p[item.start:hi.end]}", item.start)
                    items.append(Range(start=item.start, end=hi.end, lo=item.value, hi=hi.value))
                    continue
                if not self.is_js:
                    raise ParseError(
                        f"bad character range {self.p[item.start:hi.end]}", item.start)
                items.append(item)
                items.append(Char(start=save, end=save + 1, value="-"))
                items.append(hi)
                continue
            items.append(item)
        return CharClass(start=s, end=self.i, negated=negated, items=items)

    def parse_class_atom(self) -> Node:
        s = self.i
        c = self.p[self.i]
        if c == "\\":
            return self.parse_escape(in_class=True)
        if c == "[" and self.dialect == "regex" and self.startswith("[:"):
            close = self.p.find(":]", self.i)
            if close != -1:
                name = self.p[self.i + 2:close]
                self.i = close + 2
                neg = name.startswith("^")
                return Posix(start=s, end=self.i, name=name.lstrip("^"), negated=neg)
        self.i += 1
        return Char(start=s, end=self.i, value=c)

    # -- groups ------------------------------------------------------------

    def skip_balanced(self, start: int) -> int:
        """Index just past the ')' matching the '(' at `start`."""
        depth = 0
        j = start
        while j < len(self.p):
            c = self.p[j]
            if c == "\\":
                j += 2
                continue
            if c == "[":
                j += 1
                if j < len(self.p) and self.p[j] == "^":
                    j += 1
                if j < len(self.p) and self.p[j] == "]":
                    j += 1
                while j < len(self.p) and self.p[j] != "]":
                    j += 2 if self.p[j] == "\\" else 1
                j += 1
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        raise ParseError("missing ), unterminated subpattern", start)

    def read_name(self, terminator: str, start: int) -> str:
        close = self.p.find(terminator, self.i)
        if close == -1:
            raise ParseError(f"missing {terminator}, unterminated name", start)
        name = self.p[self.i:close]
        rx = _JS_NAME if self.is_js else _PY_NAME
        if not rx.fullmatch(name):
            raise ParseError(f"bad character in group name {name!r}", self.i)
        self.i = close + 1
        return name

    def finish_group(self, s: int, kind: str, header_end: int, *, index: int | None = None,
                     name: str | None = None, on: str = "", off: str = "") -> Group:
        body = self.parse_alt()
        if self.peek() != ")":
            raise ParseError("missing ), unterminated subpattern", s)
        self.i += 1
        return Group(start=s, end=self.i, kind=kind, body=body, header_end=header_end,
                     index=index, name=name, flags_on=on, flags_off=off)

    def new_capture(self, s: int, name: str | None) -> int:
        self.group_count += 1
        if name is not None:
            if name in self.group_names:
                raise ParseError(
                    f"redefinition of group name {name!r} as group {self.group_count}; "
                    f"was group {self.group_names[name]}", s)
            self.group_names[name] = self.group_count
        return self.group_count

    def parse_group(self) -> Node | None:
        s = self.i
        self.i += 1  # (
        if self.peek() != "?":
            idx = self.new_capture(s, None)
            return self.finish_group(s, "capture", self.i, index=idx)
        self.i += 1  # ?
        c = self.peek()
        if c == ":":
            self.i += 1
            return self.finish_group(s, "noncap", self.i)
        if c == "=" or c == "!":
            self.i += 1
            return self.finish_group(s, "lookahead" if c == "=" else "neg_lookahead", self.i)
        if c == ">":
            self.i += 1
            if self.is_js:
                self.issue(s, self.i, "error",
                           "atomic groups (?>...) are not supported in JavaScript")
            elif self.dialect == "python":
                self.issue(s, self.i, "warn", "atomic groups need Python 3.11 or newer")
            return self.finish_group(s, "atomic", self.i)
        if c == "<":
            if self.peek(1) in ("=", "!"):
                kind = "lookbehind" if self.peek(1) == "=" else "neg_lookbehind"
                self.i += 2
                g = self.finish_group(s, kind, self.i)
                if self.dialect == "python":
                    lo, hi = width(g.body)
                    if hi is None or lo != hi:
                        self.issue(s, g.end, "error",
                                   "Python look-behind requires a fixed-width pattern "
                                   "(error: look-behind requires fixed-width pattern)")
                return g
            self.i += 1
            if self.dialect == "python":
                self.issue(s, self.i, "error",
                           "Python does not accept (?<name>...); use (?P<name>...)")
            name = self.read_name(">", s)
            idx = self.new_capture(s, name)
            return self.finish_group(s, "named", self.i, index=idx, name=name)
        if c == "P":
            if self.peek(1) == "<":
                self.i += 2
                if self.is_js:
                    self.issue(s, self.i, "error",
                               "JavaScript does not accept (?P<name>...); use (?<name>...)")
                name = self.read_name(">", s)
                idx = self.new_capture(s, name)
                return self.finish_group(s, "named", self.i, index=idx, name=name)
            if self.peek(1) == "=":
                self.i += 2
                if self.is_js:
                    self.issue(s, self.i, "error",
                               "JavaScript does not accept (?P=name); use \\k<name>")
                name = self.read_name(")", s)
                br = Backref(start=s, end=self.i, ref=name)
                self.backrefs.append(br)
                return br
            end = self.skip_balanced(s)
            self.i = end
            return Unsupported(start=s, end=end, reason="(?P>name) subroutine calls are not modelled")
        if c == "#":
            close = self.p.find(")", self.i)
            if close == -1:
                raise ParseError("missing ), unterminated comment", s)
            text = self.p[self.i + 1:close]
            self.i = close + 1
            if self.is_js:
                self.issue(s, self.i, "error", "JavaScript has no (?#...) comments")
            return Comment(start=s, end=self.i, text=text)
        if c == "(":
            end = self.skip_balanced(s)
            self.i = end
            if self.is_js:
                self.issue(s, end, "error", "JavaScript has no conditional groups (?(id)yes|no)")
            return Unsupported(start=s, end=end,
                               reason="conditional groups (?(id)yes|no) are not explained")
        if c in ("R", "&", "|") or c.isdigit() or (c in "+-" and self.peek(1).isdigit()):
            end = self.skip_balanced(s)
            self.i = end
            return Unsupported(start=s, end=end,
                               reason="recursion / subroutine calls / branch reset are not explained")
        m = re.compile(r"([aiLmsux]*)(?:-([imsx]+))?([:)])").match(self.p, self.i)
        if m is not None and (m.group(1) or m.group(2)):
            on, off, term = m.group(1), m.group(2) or "", m.group(3)
            self.i = m.end()
            if term == ")":
                if self.is_js:
                    self.issue(s, self.i, "error",
                               "JavaScript has no global inline flags like (?i); "
                               "pass the flag separately")
                elif s != 0:
                    self.issue(s, self.i, "error",
                               "global inline flags must be at the start of the pattern "
                               "(error in Python 3.11+)")
                if off and term == ")":
                    raise ParseError("missing : after inline flags with a '-'", s)
                self.flags |= set(on)
                if "x" in on:
                    self.verbose = True
                return Flags(start=s, end=self.i, on=on, off=off)
            if self.is_js:
                self.issue(s, self.i, "warn",
                           "inline flag modifiers (?i:...) are only supported by recent "
                           "JavaScript engines (V8 12.5+, Node 23+)")
            return self.finish_group(s, "flags", self.i, on=on, off=off)
        raise ParseError(f"unknown extension (?{c}", s)


def width(node: Node) -> tuple[int, int | None]:
    """(min, max) number of characters a node can match; max None = unbounded."""
    if isinstance(node, (Char, Dot, ClassEsc, Prop, Posix, CharClass)):
        return 1, 1
    if isinstance(node, Seq):
        lo, hi = 0, 0
        unbounded = False
        for it in node.items:
            a, b = width(it)
            lo += a
            if b is None:
                unbounded = True
            else:
                hi += b
        return lo, None if unbounded else hi
    if isinstance(node, Alt):
        ws = [width(b) for b in node.branches]
        lo = min(w[0] for w in ws)
        his = [w[1] for w in ws]
        return lo, None if any(h is None for h in his) else max(h for h in his if h is not None)
    if isinstance(node, Group):
        if node.kind in ("lookahead", "neg_lookahead", "lookbehind", "neg_lookbehind"):
            return 0, 0
        return width(node.body)
    if isinstance(node, Quant):
        a, b = width(node.child)
        lo = a * node.min
        if node.max is None:
            return lo, (0 if b == 0 else None)
        return lo, None if b is None else b * node.max
    if isinstance(node, Backref):
        return 0, None
    return 0, 0


def parse(pattern: str, dialect: str = "python", flags: str = "") -> ParseResult:
    if dialect not in DIALECTS:
        raise ValueError(f"unknown dialect {dialect!r}; use one of {', '.join(DIALECTS)}")
    p = _Parser(pattern, dialect, flags)
    try:
        root = p.parse()
    except ParseError as e:
        e.pattern, e.dialect = pattern, dialect
        raise
    return ParseResult(
        root=root,
        pattern=pattern,
        dialect=dialect,
        flags="".join(sorted(p.flags)),
        issues=p.issues,
        group_count=p.group_count,
        group_names=p.group_names,
        ignored=p.ignored,
    )


def walk(node: Node) -> list[Node]:
    """All nodes in the tree, pre-order."""
    out: list[Node] = [node]
    if isinstance(node, Alt):
        for b in node.branches:
            out.extend(walk(b))
    elif isinstance(node, Seq):
        for it in node.items:
            out.extend(walk(it))
    elif isinstance(node, Group):
        out.extend(walk(node.body))
    elif isinstance(node, Quant):
        out.extend(walk(node.child))
    elif isinstance(node, CharClass):
        for it in node.items:
            out.extend(walk(it))
    return out
