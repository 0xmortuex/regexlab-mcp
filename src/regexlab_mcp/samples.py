"""Generate example strings from a parsed pattern.

Two users: suggest_tests (varied strings the pattern should match) and
check_backtracking (a prefix + repeatable unit that drives a suspect quantifier).
Generation is deterministic and structural; the real engines judge the results.
"""

from __future__ import annotations

from . import rxparse as rx
from .charsets import ALL, ALPHABET, Cx, char_set, first, sample_char

PER_NODE = 6
TOTAL = 40

Caps = dict[int | str, str]
Variant = tuple[str, Caps]


class Unsatisfiable(ValueError):
    """The generator cannot build an example for this construct."""


def _spread(items: list[Variant], limit: int) -> list[Variant]:
    """Keep at most `limit` items, evenly spread, preserving order and uniqueness."""
    seen: set[str] = set()
    uniq: list[Variant] = []
    for s, c in items:
        if s not in seen:
            seen.add(s)
            uniq.append((s, c))
    if len(uniq) <= limit:
        return uniq
    step = (len(uniq) - 1) / (limit - 1) if limit > 1 else 0
    return [uniq[round(i * step)] for i in range(limit)]


def _class_chars(node: rx.Node, cx: Cx) -> list[str]:
    """A few representative members: a readable one plus range boundaries."""
    members = char_set(node, cx)
    if not members:
        raise Unsatisfiable("a character class that matches nothing in the probe alphabet")
    out: list[str] = []
    pick = sample_char(members)
    if pick is not None:
        out.append(pick)
    if isinstance(node, rx.CharClass) and not node.negated:
        for it in node.items:
            if isinstance(it, rx.Range):
                out.extend([it.lo, it.hi])
    if isinstance(node, rx.Char):
        return [node.value]
    for c in "Z0_9 -.":  # a little variety for broad classes (\w, ., negated sets)
        if c in members and len(out) < 4 and not isinstance(node, rx.CharClass):
            out.append(c)
    seen: list[str] = []
    for c in out:
        if c not in seen and c in members:
            seen.append(c)
    return seen[:4]


def variants(node: rx.Node, cx: Cx, caps: Caps) -> list[Variant]:
    if isinstance(node, rx.Char):
        return [(node.value, {})]
    if isinstance(node, (rx.Dot, rx.ClassEsc, rx.Prop, rx.Posix, rx.CharClass)):
        return [(c, {}) for c in _class_chars(node, cx)]
    if isinstance(node, rx.Alt):
        acc: list[Variant] = []
        for b in node.branches:
            try:
                acc.extend(variants(b, cx, caps))
            except Unsatisfiable:
                continue
        if not acc:
            raise Unsatisfiable("no alternative could be generated")
        return _spread(acc, PER_NODE * 2)
    if isinstance(node, rx.Seq):
        cur: list[Variant] = [("", dict(caps))]
        for it in node.items:
            nxt: list[Variant] = []
            for s, c in cur:
                for t, d in variants(it, cx, c):
                    nxt.append((s + t, {**c, **d}))
            cur = _spread(nxt, TOTAL)
        return cur
    if isinstance(node, rx.Group):
        if node.kind in ("lookahead", "neg_lookahead", "lookbehind", "neg_lookbehind"):
            return [("", {})]
        res: list[Variant] = []
        for s, c in variants(node.body, cx, caps):
            c2 = dict(c)
            if node.index is not None:
                c2[node.index] = s
            if node.name is not None:
                c2[node.name] = s
            res.append((s, c2))
        return _spread(res, PER_NODE)
    if isinstance(node, rx.Quant):
        counts = [node.min]
        if node.max is None:
            counts += [node.min + 1, node.min + 2]
        else:
            counts += [c for c in (node.min + 1, node.max) if c <= node.max]
        counts = sorted(set(counts))
        kids = variants(node.child, cx, caps)
        res2: list[Variant] = []
        for cnt in counts:
            for s, c in kids[:3]:
                res2.append((s * cnt, c if cnt else {}))
        return _spread(res2, PER_NODE)
    if isinstance(node, rx.Backref):
        return [(caps.get(node.ref, ""), {})]
    if isinstance(node, (rx.Anchor, rx.Flags, rx.Comment)):
        return [("", {})]
    if isinstance(node, rx.Unsupported):
        raise Unsatisfiable(node.reason)
    raise Unsatisfiable(f"unhandled node {type(node).__name__}")


def positive_examples(root: rx.Alt, cx: Cx) -> list[str]:
    return [s for s, _ in variants(root, cx, {})][:TOTAL]


# -- pump generation for the backtracking probe ------------------------------


def _ancestors(root: rx.Node, target: rx.Node) -> list[rx.Node] | None:
    if root is target:
        return [root]
    for child in _children(root):
        sub = _ancestors(child, target)
        if sub is not None:
            return [root, *sub]
    return None


def _children(node: rx.Node) -> list[rx.Node]:
    if isinstance(node, rx.Alt):
        return list(node.branches)
    if isinstance(node, rx.Seq):
        return list(node.items)
    if isinstance(node, rx.Group):
        return [node.body]
    if isinstance(node, rx.Quant):
        return [node.child]
    return []



def minimal(node: rx.Node, cx: Cx, path: set[int], target: rx.Node | None,
            out: list[str], caps: Caps, ov: dict[int, int] | None = None) -> str | None:
    """Append a short string for `node` to `out`. Stops at `target` and returns its unit."""
    if node is target:
        assert isinstance(node, rx.Quant)
        unit = "".join(_once(node.child, cx, caps))
        if not unit:
            c = sample_char(first(node.child, cx)[0])
            unit = c or ""
        return unit
    if isinstance(node, rx.Seq):
        for it in node.items:
            u = minimal(it, cx, path, target, out, caps, ov)
            if u is not None:
                return u
        return None
    if isinstance(node, rx.Alt):
        chosen = node.branches[0]
        for b in node.branches:
            if id(b) in path:
                chosen = b
                break
        return minimal(chosen, cx, path, target, out, caps, ov)
    if isinstance(node, rx.Group):
        if node.kind in ("lookahead", "neg_lookahead", "lookbehind", "neg_lookbehind"):
            return None
        mark = len(out)
        u = minimal(node.body, cx, path, target, out, caps, ov)
        text = "".join(out[mark:])
        if node.index is not None:
            caps[node.index] = text
        if node.name is not None:
            caps[node.name] = text
        return u
    if isinstance(node, rx.Quant):
        reps = max(node.min, 1) if id(node) in path else node.min
        if ov is not None and id(node) in ov:
            reps = ov[id(node)]
        for _ in range(reps):
            mark = len(out)
            u = minimal(node.child, cx, path, target, out, caps, ov)
            if u is not None:
                return u
            if len(out) == mark and node.max is None:
                break
        return None
    out.extend(_once(node, cx, caps))
    return None


def _once(node: rx.Node, cx: Cx, caps: Caps) -> list[str]:
    """One short string for a node (no target)."""
    if isinstance(node, rx.Char):
        return [node.value]
    if isinstance(node, (rx.Dot, rx.ClassEsc, rx.Prop, rx.Posix, rx.CharClass)):
        c = sample_char(char_set(node, cx))
        if c is None:
            raise Unsatisfiable("a character class that matches nothing in the probe alphabet")
        return [c]
    if isinstance(node, rx.Backref):
        return [caps.get(node.ref, "")]
    if isinstance(node, (rx.Anchor, rx.Flags, rx.Comment)):
        return []
    if isinstance(node, rx.Unsupported):
        raise Unsatisfiable(node.reason)
    out: list[str] = []
    minimal(node, cx, set(), None, out, caps)
    return out


def pump_parts(root: rx.Alt, target: rx.Quant, cx: Cx) -> tuple[str, str]:
    """(prefix, unit) so that prefix + unit*n drives `target` n times."""
    chain = _ancestors(root, target) or []
    path = {id(n) for n in chain}
    out: list[str] = []
    unit = minimal(root, cx, path, target, out, {})
    if not unit:
        raise Unsatisfiable("could not build a repeatable unit for this quantifier")
    return "".join(out), unit


def pick_suffix(root: rx.Node, cx: Cx) -> str:
    """A character that nothing in the pattern matches, to make the overall match fail."""
    used: set[str] = set()
    for n in rx.walk(root):
        if isinstance(n, (rx.Char, rx.Dot, rx.ClassEsc, rx.Prop, rx.Posix, rx.CharClass)):
            used |= char_set(n, cx)
    for c in ("!", "~", "@", "#", "\x00", "Z", "é"):
        if c not in used:
            return c
    leftovers = [c for c in ALPHABET if c not in used]
    if leftovers:
        return leftovers[0]
    return "\n" if "\n" in ALL else "!"


def boundary_examples(root: rx.Alt, cx: Cx, pattern: str) -> list[tuple[str, str]]:
    """(label, text): minimal strings with one quantifier one below its minimum / above its maximum."""
    out: list[tuple[str, str]] = []
    for n in rx.walk(root):
        if not isinstance(n, rx.Quant):
            continue
        for label, reps in (("below its minimum", n.min - 1),
                            ("above its maximum", None if n.max is None else n.max + 1)):
            if reps is None or reps < 0 or reps > 60:
                continue
            if label == "below its minimum" and n.min == 0:
                continue
            chain = _ancestors(root, n) or []
            path = {id(x) for x in chain}
            buf: list[str] = []
            try:
                minimal(root, cx, path, None, buf, {}, {id(n): reps})
            except Unsatisfiable:
                continue
            out.append((f"{reps} repetition(s) of `{pattern[n.start:n.end]}`, {label}", "".join(buf)))
    return out
