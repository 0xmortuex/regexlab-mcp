"""Approximate character sets for single-character nodes.

Sets are taken over a small probe alphabet (ASCII plus a handful of interesting
non-ASCII characters). They drive the static backtracking analysis and the test
generator. They are an approximation by design: the real engines make the final call.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from . import rxparse as rx

_ASCII = [chr(c) for c in range(0x20, 0x7F)] + ["\t", "\n", "\r", "\x0b", "\x0c", "\x00"]
_EXTRA = ["é", "É", "İ", "ı", "ß", "٣", "中",
          " ", " ", "K", "ſ"]
ALPHABET: tuple[str, ...] = tuple(_ASCII + _EXTRA)
ALL = frozenset(ALPHABET)

# Order in which representative characters are preferred when we need to write one.
PREFERRED = "aA0_ b-.,:/@x1Z!~"

_JS_SPACE = set("\t\n\x0b\x0c\r       　﻿") | {
    chr(c) for c in range(0x2000, 0x200B)
}

_POSIX: dict[str, Callable[[str], bool]] = {
    "alpha": str.isalpha, "digit": str.isdigit, "alnum": str.isalnum,
    "upper": str.isupper, "lower": str.islower, "space": str.isspace,
    "punct": lambda c: unicodedata.category(c).startswith("P") or (c.isascii() and not c.isalnum() and c.isprintable() and c != " "),
    "word": lambda c: c.isalnum() or c == "_",
    "xdigit": lambda c: c in "0123456789abcdefABCDEF",
}


@dataclass(frozen=True)
class Cx:
    dialect: str = "python"
    flags: str = ""

    @property
    def js(self) -> bool:
        return self.dialect == "javascript"


def is_word(ch: str, cx: Cx) -> bool:
    if cx.js:
        return ch.isascii() and (ch.isalnum() or ch == "_")
    return ch.isalnum() or ch == "_"


def is_digit(ch: str, cx: Cx) -> bool:
    if cx.js:
        return "0" <= ch <= "9"
    return unicodedata.category(ch) == "Nd"


def is_space(ch: str, cx: Cx) -> bool:
    if cx.js:
        return ch in _JS_SPACE
    return ch.isspace()


def _prop(name: str, ch: str) -> bool:
    n = name.split("=")[-1]
    cat = unicodedata.category(ch)
    table = {
        "L": cat.startswith("L"), "Letter": cat.startswith("L"),
        "Lu": cat == "Lu", "Uppercase_Letter": cat == "Lu",
        "Ll": cat == "Ll", "Lowercase_Letter": cat == "Ll",
        "N": cat.startswith("N"), "Number": cat.startswith("N"),
        "Nd": cat == "Nd", "Decimal_Number": cat == "Nd",
        "P": cat.startswith("P"), "Punctuation": cat.startswith("P"),
        "S": cat.startswith("S"), "Symbol": cat.startswith("S"),
        "Z": cat.startswith("Z"), "Separator": cat.startswith("Z"),
        "Alphabetic": ch.isalpha(), "White_Space": ch.isspace(),
        "ASCII": ch.isascii(),
    }
    return table.get(n, False)


def _base(node: rx.Node, cx: Cx) -> set[str]:
    if isinstance(node, rx.Char):
        return {node.value}
    if isinstance(node, rx.Dot):
        if "s" in cx.flags:
            return set(ALPHABET)
        nl = {"\n", "\r", " ", " "} if cx.js else {"\n"}
        return {c for c in ALPHABET if c not in nl}
    if isinstance(node, rx.ClassEsc):
        fn = {"d": is_digit, "w": is_word, "s": is_space}[node.letter.lower()]
        inside = {c for c in ALPHABET if fn(c, cx)}
        return set(ALPHABET) - inside if node.letter.isupper() else inside
    if isinstance(node, rx.Prop):
        inside = {c for c in ALPHABET if _prop(node.name, c)}
        return set(ALPHABET) - inside if node.negated else inside
    if isinstance(node, rx.Posix):
        fn2 = _POSIX.get(node.name)
        inside = {c for c in ALPHABET if fn2 is not None and fn2(c)}
        return set(ALPHABET) - inside if node.negated else inside
    if isinstance(node, rx.Range):
        return {c for c in ALPHABET if node.lo <= c <= node.hi}
    if isinstance(node, rx.CharClass):
        acc: set[str] = set()
        for it in node.items:
            acc |= _base(it, cx)
        acc = _fold(acc, cx)
        if node.negated:
            acc = set(ALPHABET) - acc
        return acc
    return set()


def _fold(chars: set[str], cx: Cx) -> set[str]:
    if "i" not in cx.flags:
        return chars
    out = set(chars)
    for c in chars:
        for v in (c.lower(), c.upper(), c.swapcase()):
            if len(v) == 1:
                out.add(v)
    return out


def char_set(node: rx.Node, cx: Cx) -> frozenset[str]:
    """Characters from ALPHABET that a single-character node can match."""
    s = _base(node, cx)
    if not isinstance(node, rx.CharClass):  # classes fold before negating, inside _base
        s = _fold(s, cx)
    if isinstance(node, rx.Char):
        return frozenset(s)
    return frozenset(s) & ALL


SINGLE = (rx.Char, rx.Dot, rx.ClassEsc, rx.Prop, rx.Posix, rx.CharClass)


def first(node: rx.Node, cx: Cx) -> tuple[frozenset[str], bool]:
    """(characters a match can start with, can the node match the empty string)."""
    if isinstance(node, SINGLE):
        return char_set(node, cx), False
    if isinstance(node, rx.Seq):
        acc: set[str] = set()
        for it in node.items:
            s, nullable = first(it, cx)
            acc |= s
            if not nullable:
                return frozenset(acc), False
        return frozenset(acc), True
    if isinstance(node, rx.Alt):
        acc2: set[str] = set()
        nul = False
        for b in node.branches:
            s, n = first(b, cx)
            acc2 |= s
            nul = nul or n
        return frozenset(acc2), nul
    if isinstance(node, rx.Group):
        if node.kind in ("lookahead", "neg_lookahead", "lookbehind", "neg_lookbehind"):
            return frozenset(), True
        return first(node.body, cx)
    if isinstance(node, rx.Quant):
        s, n = first(node.child, cx)
        return s, n or node.min == 0
    if isinstance(node, rx.Backref):
        return ALL, True
    return frozenset(), True  # anchors, flags, comments, unsupported


def sample_char(chars: frozenset[str]) -> str | None:
    """A readable member of the set, or None when the set is empty."""
    for c in PREFERRED:
        if c in chars:
            return c
    printable = sorted(c for c in chars if c.isprintable())
    if printable:
        return printable[0]
    return min(chars) if chars else None
