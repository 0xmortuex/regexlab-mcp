"""regexlab-mcp: an MCP server that runs regular expressions in real engines."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import backtrack, compare, convert, explain, replace, suggest, testrun
from . import engines as eng
from . import rxparse as rx
from .sandbox import SandboxError

mcp = MCPServer(
    "regexlab",
    instructions=(
        "A regex lab that RUNS patterns in real engines instead of guessing: Python `re`, "
        "JavaScript RegExp (via node, when installed) and the optional Python `regex` module. "
        "Always verify a regex with test_regex before presenting it as working. Use "
        "explain_regex to read a pattern, check_backtracking before putting a pattern near "
        "untrusted input (it measures ReDoS in a killable child process), compare_engines "
        "when a pattern must work in more than one language, convert_pattern to translate "
        "between Python and JavaScript syntax, replace_preview before running a substitution, "
        "and suggest_tests to get verified should-match / should-not-match examples. Spans are "
        "in Unicode code points. Tools say which engines were skipped and why."
    ),
)

F = TypeVar("F", bound=Callable[..., Any])

# mcp>=2 only forwards a ToolError's message to the client. These are failures an agent
# can act on (bad pattern, missing node, limits); anything else is a real bug and stays
# hidden behind the generic error.
_EXPECTED = (ValueError, SandboxError)


def tool(fn: F) -> F:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except rx.ParseError as e:
            raise ToolError(e.detail()) from e
        except _EXPECTED as e:
            raise ToolError(str(e)) from e

    mcp.tool()(wrapper)
    return cast(F, wrapper)  # direct callers see the same ToolError as MCP clients


def _subjects(subject: str | list[str]) -> list[str]:
    return [subject] if isinstance(subject, str) else list(subject)


@tool
def test_regex(
    pattern: str,
    subject: str | list[str],
    flags: str = "",
    engines: list[str] | None = None,
    timeout_s: float = 5.0,
    max_matches: int = 100,
) -> str:
    """Run a regex against text in REAL engines and report every match.

    Use this to check any regex before telling the user it works. For each engine you
    get all matches (global search) with [start:end) spans in Unicode code points, the
    numbered and named capture groups, and a flag when engines disagree.

    pattern: the regex source, exactly as it would appear in code (no delimiters, no
      extra string escaping).
    subject: one text, or a list of texts; each list item is a separate case.
    flags: letters: i ignore case, m multiline, s dot matches newline, x verbose and
      a ASCII classes (Python only), u unicode, v unicodeSets and y sticky (JavaScript
      only). g is implied.
    engines: any of "python", "javascript" (or "js"), "regex". Default: every engine
      available on this machine; unavailable ones are reported, not silently skipped.
    timeout_s: hard limit (0.2-30). A run that exceeds it is killed and reported as a
      catastrophic-backtracking case.
    max_matches: cap per case (max 500).
    Limits: pattern 5000 chars, 100 cases, 200k characters of subject in total.
    """
    picked, notes = eng.pick_engines(engines)
    subs = _subjects(subject)
    eng.check_pattern(pattern)
    eng.check_subjects(subs)
    eng.check_timeout(timeout_s)
    return testrun.test(pattern, subs, flags, picked, timeout_s=timeout_s,
                        max_matches=max_matches, notes=notes)


@tool
def explain_regex(pattern: str, engine: str = "python", flags: str = "") -> str:
    """Explain a regex token by token in plain English, using this server's own parser.

    Covers literals, escapes, classes, quantifiers (greedy, lazy, possessive), groups
    (capturing, named, non-capturing, atomic), lookahead and lookbehind, backreferences,
    anchors and inline flags, and says how each construct behaves in the chosen engine
    (for example Python's `$` before a trailing newline, or JavaScript's ASCII-only
    \\d). Constructs it does not model (recursion, conditionals, \\N{...}) are marked
    "NOT EXPLAINED" rather than guessed. Syntax errors are reported with the position.
    Engine-specific syntax that the engine would reject is flagged as ERROR.

    engine: "python" (default), "javascript" or "regex" (syntax dialect only; this tool
      does not need the engine installed).
    flags: same letters as test_regex; they change the explanation (m, s, a, x, ...).
    """
    key = eng.ALIASES.get(engine.strip().lower())
    if key is None:
        raise ValueError(f"unknown engine {engine!r}; use python, javascript (or js), or regex")
    eng.check_pattern(pattern)
    parsed = rx.parse(pattern, key, eng.normalize_flags(flags))
    return explain.explain(parsed)


@tool
def check_backtracking(
    pattern: str, engine: str = "python", flags: str = "", timeout_s: float = 3.0
) -> str:
    """Check a regex for catastrophic backtracking (ReDoS) by analysing AND timing it.

    Two stages. Static: finds nested quantifiers like (a+)+, alternation with overlapping
    branches inside a loop, adjacent overlapping repeats like .*.*, and unanchored leading
    repeats. Empirical: builds adversarial input for the worst suspects and times it in
    the real engine in a child process that is KILLED at the time limit. A pattern that
    hits the limit is reported as exponential, with an example slow input. Static
    suspicions the probe cannot reproduce are reported as unconfirmed; "no finding" is
    evidence, not proof.

    engine: "python" (default), "javascript", "regex", or "all" for every available
      engine (V8 and Python's re have different optimisations, so results can differ).
    timeout_s: time budget per probe run, 0.2-30 (default 3).
    """
    eng.check_timeout(timeout_s)
    picked, notes = eng.pick_engines("all" if engine.strip().lower() == "all" else [engine])
    return backtrack.check(pattern, flags, picked, timeout_s=timeout_s, notes=notes)


@tool
def compare_engines(
    pattern: str, subject: str, flags: str = "", timeout_s: float = 5.0
) -> str:
    """Run one pattern in every available engine and explain where they differ.

    Reports whether the pattern compiles in each engine (named-group syntax (?P<n> vs
    (?<n>, \\A \\Z, inline flags, atomic groups, lookbehind width, \\p{..}), runs your
    subject plus targeted variants (trailing newline, Unicode digits, non-ASCII word,
    emoji, Turkish case pairs) in all engines, and lists the known semantic differences
    that apply to this pattern ($ and trailing newline, Unicode-aware \\d \\w \\b,
    dot and line terminators, case folding). Use it when a regex has to behave the same
    in Python and JavaScript.
    """
    eng.check_timeout(timeout_s)
    picked, notes = eng.pick_engines(None)
    if len(picked) < 2:
        notes.append("only one engine is available, so nothing can be compared at run time; "
                     "the syntax-level findings above still apply")
    return compare.compare(pattern, subject, flags, picked, timeout_s=timeout_s, notes=notes)


@tool
def convert_pattern(
    pattern: str, from_engine: str, to_engine: str, flags: str = "", timeout_s: float = 5.0
) -> str:
    """Translate a regex between Python and JavaScript syntax, with explicit warnings.

    Handles named groups and named backreferences, \\A \\Z and $ (using exact lookaround
    equivalents), inline global flags, verbose mode, {,n}, \\U and \\u{...} escapes, [^]
    and [], and the g/y/u flags. Things with no equivalent (atomic groups, possessive
    quantifiers, \\p{..} in Python re, POSIX classes, recursion) are left unchanged and
    listed as WARNINGS. Differences that cannot be fixed syntactically (Unicode-aware
    \\d \\w vs ASCII-only) are listed too. The result is compiled in the target engine
    to prove it is at least valid; run test_regex or compare_engines to prove it is
    equivalent.

    from_engine / to_engine: "python", "javascript" (or "js"), "regex".
    flags: flags of the source pattern; the converted flags are returned.
    """
    eng.check_timeout(timeout_s)
    return convert.convert(pattern, from_engine, to_engine, flags, timeout_s=timeout_s)


@tool
def replace_preview(
    pattern: str,
    replacement: str,
    subject: str,
    flags: str = "",
    engines: list[str] | None = None,
    timeout_s: float = 5.0,
    max_matches: int = 100,
) -> str:
    """Preview a regex substitution: every match, what it becomes, and the final text.

    The replacement is expanded by each engine's own template rules, so the preview shows
    what the code will really do. Python uses \\1, \\g<1> and \\g<name>; JavaScript uses
    $1, $<name>, $&, $` , $' and $$. If the replacement uses the other language's syntax
    (a common bug: "$1" in Python is literal text) a WARNING says so, and differing
    results between engines are called out.

    engines: any of "python", "javascript" (or "js"), "regex"; default all available.
    """
    picked, notes = eng.pick_engines(engines)
    eng.check_timeout(timeout_s)
    return replace.preview(pattern, replacement, subject, flags, picked, timeout_s=timeout_s,
                           max_matches=max_matches, notes=notes)


@tool
def suggest_tests(
    pattern: str, engine: str = "python", flags: str = "", timeout_s: float = 5.0
) -> str:
    """Generate should-match and should-not-match test strings for a regex, verified by running.

    Builds examples from the pattern's structure (each alternative, quantifier minimum
    and maximum, class boundaries) plus mutations (deleted or replaced characters, junk
    prefix/suffix, trailing newline, case swap, off-by-one repeat counts), then RUNS all of
    them in the real engine. Only confirmed examples are listed as tests. Mutations that
    unexpectedly still match are listed as SURPRISES - they often expose a missing anchor,
    a `$` that accepts a trailing newline, or a class that is too broad. Uses search
    semantics (match anywhere), and says when the pattern is not anchored.

    engine: "python" (default), "javascript" or "regex" (must be available).
    """
    eng.check_timeout(timeout_s)
    picked, notes = eng.pick_engines([engine])
    return suggest.suggest(pattern, picked[0], flags, timeout_s=timeout_s, notes=notes)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
