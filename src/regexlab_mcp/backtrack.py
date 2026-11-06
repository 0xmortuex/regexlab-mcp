"""check_backtracking: static heuristics plus an empirical timing probe.

The static pass finds quantifier shapes that can backtrack badly (nested and
overlapping repeats, ambiguous alternation in a loop, adjacent overlapping repeats,
an unanchored leading repeat). The probe then builds adversarial input for the top
suspects and times them in the real engine inside a killable child process.
Static findings are suspicions; only the probe measures anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import engines as eng
from . import rxparse as rx
from .charsets import Cx, first
from .samples import Unsatisfiable, pick_suffix, positive_examples, pump_parts

EXP_SIZES = [8, 12, 16, 20, 24, 28, 32, 36, 40]
POLY_SIZES = [1000, 2000, 4000, 8000, 16000, 32000]
STOP_MS = 400.0
MAX_PROBED_HOTSPOTS = 2


@dataclass
class Hotspot:
    kind: str  # nested, alternation, adjacent, leading
    target: rx.Quant
    description: str
    exponential: bool
    detail: str = ""

    @property
    def rank(self) -> int:
        return {"nested": 0, "alternation": 1, "adjacent": 2, "leading": 3}[self.kind]


def _big(q: rx.Quant) -> bool:
    return q.max is None or q.max > 10


def _snip(pattern: str, node: rx.Node) -> str:
    s = pattern[node.start:node.end]
    return s if len(s) <= 40 else s[:37] + "..."


def _seq_first(items: list[rx.Node], cx: Cx) -> tuple[frozenset[str], bool]:
    acc: set[str] = set()
    for it in items:
        s, nullable = first(it, cx)
        acc |= s
        if not nullable:
            return frozenset(acc), False
    return frozenset(acc), True


class _Static:
    def __init__(self, parsed: rx.ParseResult, cx: Cx) -> None:
        self.pattern = parsed.pattern
        self.cx = cx
        self.spots: list[Hotspot] = []
        self._seen: set[tuple[str, int]] = set()

    def add(self, spot: Hotspot) -> None:
        key = (spot.kind, id(spot.target))
        if key not in self._seen:
            self._seen.add(key)
            self.spots.append(spot)

    def run(self, root: rx.Alt) -> None:
        self.walk(root, frozenset(), True, [], False)
        self.leading(root)

    def walk(self, node: rx.Node, follow: frozenset[str], follow_nullable: bool,
             loops: list[rx.Quant], atomic: bool) -> None:
        cx = self.cx
        if isinstance(node, rx.Alt):
            if loops and not atomic:
                self.check_alternation(node, loops[-1])
            for b in node.branches:
                self.walk(b, follow, follow_nullable, loops, atomic)
        elif isinstance(node, rx.Seq):
            for i, it in enumerate(node.items):
                rest, nullable = _seq_first(node.items[i + 1:], cx)
                if nullable:
                    rest = rest | follow
                    nullable = follow_nullable
                self.walk(it, rest, nullable, loops, atomic)
            if not atomic:
                self.check_adjacent(node)
        elif isinstance(node, rx.Group):
            inner_atomic = atomic or node.kind == "atomic"
            self.walk(node.body, follow, follow_nullable, loops, inner_atomic)
        elif isinstance(node, rx.Quant):
            backtracks = node.mode != "possessive" and not atomic
            if backtracks and _big(node) and loops:
                self.check_nested(node, follow, loops[-1])
            child_first, _ = first(node.child, cx)
            again = child_first if (node.max is None or node.max > 1) else frozenset()
            nxt = loops + [node] if backtracks and _big(node) else loops
            self.walk(node.child, follow | again, follow_nullable, nxt,
                      atomic or node.mode == "possessive")

    def check_nested(self, inner: rx.Quant, follow: frozenset[str], loop: rx.Quant) -> None:
        inner_chars, _ = first(inner.child, self.cx)
        if inner_chars & follow:
            self.add(Hotspot(
                "nested", loop,
                f"`{_snip(self.pattern, loop)}` repeats something that contains the repeat "
                f"`{_snip(self.pattern, inner)}`, and what follows that inner repeat can start "
                "with the same characters. One input can be split between the iterations in "
                "exponentially many ways.", True))

    def check_alternation(self, alt: rx.Alt, loop: rx.Quant) -> None:
        sets = [first(b, self.cx)[0] for b in alt.branches]
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                if sets[i] & sets[j]:
                    a, b = alt.branches[i], alt.branches[j]
                    self.add(Hotspot(
                        "alternation", loop,
                        f"alternation `{_snip(self.pattern, alt)}` sits inside the repeat "
                        f"`{_snip(self.pattern, loop)}` and its branches `{_snip(self.pattern, a)}` "
                        f"and `{_snip(self.pattern, b)}` can start with the same characters. "
                        "If they can match the same text this backtracks exponentially.", True))
                    return

    def check_adjacent(self, seq: rx.Seq) -> None:
        items = seq.items
        for i, a in enumerate(items):
            if not (isinstance(a, rx.Quant) and a.max is None and a.mode != "possessive"):
                continue
            a_chars, _ = first(a.child, self.cx)
            for b in items[i + 1:]:
                if isinstance(b, rx.Quant) and b.max is None and b.mode != "possessive":
                    b_chars, _ = first(b.child, self.cx)
                    if a_chars & b_chars:
                        self.add(Hotspot(
                            "adjacent", a,
                            f"adjacent repeats `{_snip(self.pattern, a)}` and "
                            f"`{_snip(self.pattern, b)}` can match the same characters, so a failing "
                            "match tries every way to divide the text between them (polynomial, "
                            "O(n^2) or worse).", False))
                _, nullable = first(b, self.cx)
                if not nullable:
                    break

    def leading(self, root: rx.Alt) -> None:
        for branch in root.branches:
            items = branch.items
            k = 0
            while k < len(items) and isinstance(items[k], (rx.Flags, rx.Comment)):
                k += 1
            if k >= len(items):
                continue
            head = items[k]
            if isinstance(head, rx.Anchor) and (head.kind == "abs_start" or (
                    head.kind == "bol" and "m" not in self.cx.flags)):
                continue
            if isinstance(head, rx.Quant) and head.max is None and head.mode != "possessive" \
                    and len(items) > k + 1:
                self.add(Hotspot(
                    "leading", head,
                    f"the pattern is not anchored and starts with the repeat "
                    f"`{_snip(self.pattern, head)}`; on input where the rest fails to match, the "
                    "engine retries from every start position (quadratic).", False))


def static_scan(parsed: rx.ParseResult, cx: Cx) -> list[Hotspot]:
    s = _Static(parsed, cx)
    s.run(parsed.root)
    return sorted(s.spots, key=lambda h: h.rank)


# -- empirical probe -----------------------------------------------------------


@dataclass
class ProbeVerdict:
    engine: str
    level: str  # exponential, polynomial, none, error, skipped
    text: str
    example: str = ""
    seconds: float = 0.0


@dataclass
class HotspotReport:
    spot: Hotspot | None
    verdicts: list[ProbeVerdict] = field(default_factory=list)
    note: str = ""


def render_input(prefix: str, unit: str, n: int, suffix: str) -> str:
    parts: list[str] = []
    if prefix:
        parts.append(repr(prefix))
    parts.append(f"{unit!r} * {n}")
    parts.append(repr(suffix))
    return " + ".join(parts)


def _exp_ratio(points: list[eng.ProbePoint]) -> float:
    if len(points) < 2 or points[-2].ms <= 0.05:
        return 0.0
    return points[-1].ms / points[-2].ms


def _classify(engine: str, run: eng.EngineRun, prefix: str, unit: str, suffix: str,
              has_exp: bool, budget: float) -> ProbeVerdict:
    if run.error:
        return ProbeVerdict(engine, "error", f"{engine} could not run the pattern: {run.error}")
    exp_pts = [p for p in run.probe if p.group == "exp"]
    poly_pts = [p for p in run.probe if p.group == "poly"]
    if run.timed_out:
        if has_exp and not poly_pts:
            n = exp_pts[-1].n + 4 if exp_pts else EXP_SIZES[0]
            last = f"; last completed n={exp_pts[-1].n} took {exp_pts[-1].ms:.0f} ms" if exp_pts else ""
            return ProbeVerdict(
                engine, "exponential",
                f"{engine}: TIMED OUT after {budget:.1f}s and was killed, at n~{n}{last}. "
                "Time roughly multiplies with each extra character: exponential backtracking.",
                render_input(prefix, unit, n, suffix), run.elapsed)
        n = (poly_pts[-1].n * 2) if poly_pts else POLY_SIZES[0]
        return ProbeVerdict(
            engine, "polynomial",
            f"{engine}: TIMED OUT after {budget:.1f}s and was killed, at n~{n} "
            f"(input length ~{n * len(unit)}); strongly super-linear.",
            render_input(prefix, unit, n, suffix), run.elapsed)
    if has_exp and exp_pts and exp_pts[-1].ms > STOP_MS and _exp_ratio(exp_pts) >= 4:
        p = exp_pts[-1]
        return ProbeVerdict(
            engine, "exponential",
            f"{engine}: {p.ms:.0f} ms at n={p.n} ({_exp_ratio(exp_pts):.0f}x slower than "
            f"n={exp_pts[-2].n}): exponential growth.",
            render_input(prefix, unit, p.n, suffix), run.elapsed)
    if len(poly_pts) >= 2:
        a, b = poly_pts[-2], poly_pts[-1]
        if b.ms >= 100 and a.ms > 0:
            k = math.log(b.ms / a.ms) / math.log(b.n / a.n)
            if k >= 1.5:
                return ProbeVerdict(
                    engine, "polynomial",
                    f"{engine}: {b.ms:.0f} ms at n={b.n}, growing ~n^{k:.1f} "
                    f"(doubling the input multiplied the time by {b.ms / a.ms:.1f}x).",
                    render_input(prefix, unit, b.n, suffix), run.elapsed)
    worst = max((p.ms for p in run.probe), default=0.0)
    return ProbeVerdict(engine, "none",
                        f"{engine}: no slowdown on the crafted inputs (slowest {worst:.1f} ms).",
                        seconds=run.elapsed)


def probe_hotspot(parsed: rx.ParseResult, cx: Cx, spot: Hotspot, engines: list[str],
                  flags: str, budget: float) -> list[ProbeVerdict]:
    prefix, unit = pump_parts(parsed.root, spot.target, cx)
    suffix = pick_suffix(parsed.root, cx)
    return _probe(parsed.pattern, flags, engines, prefix, unit, suffix, spot.exponential, budget)


def _probe(pattern: str, flags: str, engines: list[str], prefix: str, unit: str, suffix: str,
           has_exp: bool, budget: float) -> list[ProbeVerdict]:
    inputs: list[dict[str, object]] = []
    if has_exp:
        inputs += [{"group": "exp", "prefix": prefix, "unit": unit, "n": n, "suffix": suffix}
                   for n in EXP_SIZES]
    inputs += [{"group": "poly", "prefix": prefix, "unit": unit, "n": n, "suffix": suffix}
               for n in POLY_SIZES if len(unit) * n <= 100_000]
    out: list[ProbeVerdict] = []
    for e in engines:
        run = eng.run_probe(e, pattern, flags, inputs, timeout=budget, stop_ms=STOP_MS)
        out.append(_classify(e, run, prefix, unit, suffix, has_exp, budget))
    return out


def check(pattern: str, flags: str, engines: list[str], *, timeout_s: float,
          notes: list[str]) -> str:
    eng.check_pattern(pattern)
    flags = eng.normalize_flags(flags)
    dialect = engines[0]
    parsed = rx.parse(pattern, dialect, flags)
    cx = Cx(dialect, parsed.flags)
    spots = static_scan(parsed, cx)

    reports: list[HotspotReport] = []
    for spot in spots[:MAX_PROBED_HOTSPOTS]:
        rep = HotspotReport(spot)
        try:
            rep.verdicts = probe_hotspot(parsed, cx, spot, engines, flags, timeout_s)
        except Unsatisfiable as e:
            rep.note = f"not probed: {e}"
        reports.append(rep)
        if any(v.level == "exponential" for v in rep.verdicts):
            break  # already proven catastrophic; further probes only burn time
    for spot in spots[len(reports):]:
        reports.append(HotspotReport(spot, note="not probed (probe budget; the most severe "
                                                "suspects were probed first)"))
    generic: list[ProbeVerdict] = []
    generic_note = ""
    if not spots:
        try:
            sample = positive_examples(parsed.root, cx)[0]
            unit = sample or "a"
            generic = _probe(pattern, flags, engines, "", unit, pick_suffix(parsed.root, cx),
                             False, timeout_s)
        except (Unsatisfiable, IndexError) as e:
            generic_note = f"generic probe skipped: {e}"

    confirmed = [(v.engine, v) for r in reports for v in r.verdicts
                 if v.level in ("exponential", "polynomial")]
    lines: list[str] = [f"Pattern: {pattern}" + (f"   flags: {flags}" if flags else ""),
                        f"Engines probed: {', '.join(engines)}; budget {timeout_s:.1f}s per probe run, "
                        "killed on expiry.", ""]
    if any(v.level == "exponential" for _, v in confirmed):
        lines.append("RISK: HIGH - catastrophic (exponential) backtracking CONFIRMED by running it.")
    elif confirmed:
        lines.append("RISK: MEDIUM - super-linear (polynomial) backtracking CONFIRMED by running it.")
    elif spots:
        lines.append("RISK: LOW/UNCONFIRMED - the static scan found suspicious shapes, but the "
                     "timing probe did not reproduce a slowdown (see below).")
    else:
        lines.append("RISK: none found - no risky quantifier shapes, and the generic probe was "
                     "fast. This is evidence, not a proof of safety.")
    lines.append("")
    if spots:
        lines.append("Static findings (suspicions from the pattern's structure):")
        for i, r in enumerate(reports, 1):
            assert r.spot is not None
            lines.append(f"  {i}. [{r.spot.kind}] {r.spot.description}")
            if r.note:
                lines.append(f"     {r.note}")
            for v in r.verdicts:
                lines.append(f"     probe: {v.text}")
                if v.example:
                    lines.append(f"     slow input: {v.example}")
        lines.append("")
    else:
        lines.append("Static findings: no nested/overlapping quantifier, no ambiguous alternation "
                     "in a loop, no adjacent overlapping repeats.")
        for v in generic:
            lines.append(f"  generic probe: {v.text}")
        if generic_note:
            lines.append(f"  {generic_note}")
        lines.append("")
    if confirmed:
        lines.append("Fixes: make the repeated parts unambiguous (so each character can only be "
                     "consumed one way), use a possessive quantifier or atomic group where the "
                     "engine supports it (Python 3.11+, regex module; not JavaScript), anchor the "
                     "pattern, bound the repeat counts, or check the length of the input first.")
    for n in notes:
        lines.append(f"Note: {n}")
    return "\n".join(lines).rstrip()
