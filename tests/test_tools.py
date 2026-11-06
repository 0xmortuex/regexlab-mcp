"""test_regex, compare_engines, convert_pattern, replace_preview, suggest_tests end to end.

Imports the server module (not the tool functions) so pytest does not collect the
tool named test_regex as a test.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from helpers import HAS_NODE, needs_node
from regexlab_mcp import engines as eng
from regexlab_mcp import server


# -- test_regex ----------------------------------------------------------------


def test_test_regex_reports_matches_groups_and_spans():
    out = server.test_regex(r"(?P<y>\d{4})-(\d\d)", ["on 2024-05", "nothing"], engines=["python"])
    assert "case 1: 'on 2024-05'" in out
    assert "[3:10] '2024-05'" in out
    assert "1='2024'@3:7 2='05'@8:10" in out
    assert "named: y='2024'" in out
    assert "case 2: 'nothing'" in out and "python: no match" in out


@needs_node
def test_test_regex_flags_engine_disagreement_side_by_side():
    out = server.test_regex(r"^\d+$", "123\n")
    assert "python: 1 match" in out and "javascript: no match" in out
    assert "ENGINES DIFFER" in out


@needs_node
def test_test_regex_reports_a_compile_error_in_one_engine_only():
    out = server.test_regex(r"(?P<x>a)", "a")
    assert "javascript: ERROR (compile)" in out
    assert "python: 1 match" in out


def test_test_regex_timeout_is_reported_with_the_stuck_case():
    out = server.test_regex(r"(a+)+$", ["ok aaa", "a" * 60 + "!"], engines=["python"], timeout_s=1.5)
    assert "TIMED OUT" in out and "case 2 of 2" in out
    assert "#1 [3:6] 'aaa'" in out  # case 1 finished before the hang


def test_test_regex_rejects_bad_input_with_actionable_errors():
    with pytest.raises(ToolError, match="unknown flag"):
        server.test_regex("a", "a", flags="Q")
    with pytest.raises(ToolError, match="no subject"):
        server.test_regex("a", [])
    with pytest.raises(ToolError, match="timeout_s"):
        server.test_regex("a", "a", timeout_s=999)
    with pytest.raises(ToolError, match="unknown engine"):
        server.test_regex("a", "a", engines=["perl"])


def test_test_regex_unavailable_engine_is_explicit(monkeypatch):
    monkeypatch.setattr(eng, "node_path", lambda: None)
    with pytest.raises(ToolError, match="javascript is unavailable"):
        server.test_regex("a", "a", engines=["js"])
    out = server.test_regex("a", "a")
    assert "javascript: skipped" in out


# -- compare_engines -------------------------------------------------------------


@needs_node
def test_compare_engines_finds_the_dollar_and_unicode_digit_differences():
    out = server.compare_engines(r"^\d+$", "123")
    assert "DIFFER" in out
    assert "your subject + a trailing newline" in out
    assert "Arabic-Indic" in out
    assert "`$` (no m flag)" in out and "`\\d`" in out


@needs_node
def test_compare_engines_reports_named_group_and_flag_syntax():
    out = server.compare_engines(r"(?i)(?P<n>a)", "A")
    assert "javascript: NO (compile)" in out
    assert "(?<name>" in out or "global inline flags" in out
    assert "python: yes" in out


@needs_node
def test_compare_engines_lookbehind_and_emoji():
    out = server.compare_engines(r"(?<=a+).", "aab")
    assert "python: NO (compile)" in out
    assert "Lookbehind" in out
    out2 = server.compare_engines(r".", "x")
    assert "emoji" in out2 and "DIFFER" in out2


# -- convert_pattern -------------------------------------------------------------


@needs_node
def test_convert_python_to_javascript_is_verified_in_node():
    out = server.convert_pattern(r"(?P<y>\d{4})-(?P=y)\Z", "python", "javascript")
    assert r"(?<y>\d{4})-\k<y>(?![\s\S])" in out
    assert "compiles in javascript" in out


@needs_node
def test_converted_pattern_behaves_the_same_on_real_input():
    py_pat = r"(?P<w>\w+)@(?P=w)\Z"
    js_pat = r"(?<w>\w+)@\k<w>(?![\s\S])"
    assert r"(?<w>\w+)@\k<w>(?![\s\S])" in server.convert_pattern(py_pat, "python", "js")
    subjects = ["ab@ab", "ab@ab\n", "ab@abc", "x ab@ab"]
    py = eng.run_match("python", py_pat, "", subjects)
    js = eng.run_match("javascript", js_pat, "", subjects)
    assert [[(m.start, m.end) for m in c.matches] for c in py.cases] == \
           [[(m.start, m.end) for m in c.matches] for c in js.cases]


@needs_node
def test_convert_dollar_keeps_python_semantics_exactly():
    out = server.convert_pattern(r"^abc$", "python", "javascript")
    assert r"(?=\n?(?![\s\S]))" in out
    js_pat = r"^abc(?=\n?(?![\s\S]))"
    subjects = ["abc", "abc\n", "abc\n\n", "abcd"]
    py = eng.run_match("python", r"^abc$", "", subjects)
    js = eng.run_match("javascript", js_pat, "", subjects)
    assert [bool(c.matches) for c in py.cases] == [bool(c.matches) for c in js.cases] \
        == [True, True, False, False]


@needs_node
def test_convert_inline_flags_and_verbose_and_brace_quantifier():
    out = server.convert_pattern("(?i)a{,2}", "python", "javascript")
    assert "pattern: a{0,2}" in out and "flags:   i" in out
    out2 = server.convert_pattern("a b # c\n d", "python", "javascript", flags="x")
    assert "pattern: abd" in out2


def test_convert_unconvertible_constructs_are_warnings_and_fail_verification():
    out = server.convert_pattern(r"(?>a+)b", "python", "javascript")
    assert "WARNINGS" in out and "atomic group" in out
    if HAS_NODE:
        assert "does NOT compile in javascript" in out


@needs_node
def test_convert_javascript_to_python():
    out = server.convert_pattern(r"(?<y>\d+)\k<y>$ [^]", "javascript", "python")
    assert r"(?P<y>[0-9]+)" not in out  # \d is not rewritten; the a flag is used instead
    assert r"(?P<y>\d+)(?P=y)\Z [\s\S]" in out
    assert "flags:   a" in out and "compiles in python" in out


def test_convert_same_dialect_is_a_noop_and_unknown_engine_errors():
    assert "nothing to convert" in server.convert_pattern("a+", "python", "python")
    with pytest.raises(ToolError, match="unknown engine"):
        server.convert_pattern("a", "python", "perl")


# -- replace_preview -------------------------------------------------------------


def test_replace_preview_python_template_syntax():
    out = server.replace_preview(r"(\w+)@(\w+)", r"\2 at \1", "bob@home, al@work", engines=["python"])
    assert "#1 [0:8] 'bob@home'  ->  'home at bob'" in out
    assert "result: 'home at bob, work at al'" in out


@needs_node
def test_replace_preview_javascript_template_syntax():
    out = server.replace_preview(r"(\w+)@(\w+)", "$2 at $1 [$&] $$", "bob@home", engines=["js"])
    assert "'home at bob [bob@home] $'" in out


@needs_node
def test_replace_preview_warns_when_the_other_languages_syntax_is_used():
    out = server.replace_preview(r"(\w+)", "<$1>", "ab")
    assert "WARNING" in out and "JavaScript-style tokens" in out
    assert "RESULTS DIFFER" in out
    py = [ln for ln in out.splitlines() if ln.strip().startswith("result:")]
    assert any("<$1>" in ln for ln in py)  # python left $1 as literal text


def test_replace_preview_bad_group_reference_is_reported():
    out = server.replace_preview(r"(a)", r"\3", "a", engines=["python"])
    assert "invalid group reference 3" in out


# -- suggest_tests ---------------------------------------------------------------


def test_suggest_tests_examples_are_verified_and_surprises_surface():
    out = server.suggest_tests(r"^[A-Z]{2}\d{3,4}$")
    assert "SHOULD MATCH" in out and "'AA000'" in out
    assert "SHOULD NOT MATCH" in out and "'A000'" in out
    assert "SURPRISES" in out and r"'AA000\n'" in out  # Python's $ accepts a trailing newline
    assert "not anchored" not in out


def test_suggest_tests_every_listed_example_really_behaves_as_listed():
    pattern = r"^(?:\+90|0)?5\d{9}$"
    out = server.suggest_tests(pattern)
    section = None
    for line in out.splitlines():
        if line.startswith("SHOULD MATCH"):
            section = True
            continue
        if line.startswith("SHOULD NOT MATCH"):
            section = False
            continue
        if line.startswith("SURPRISES") or not line.startswith("  "):
            section = None if line.startswith("SURPRISES") else section
            continue
        if section is None:
            continue
        literal = line.strip().split("  ")[0]
        text = eval(literal, {})  # noqa: S307 - our own repr() output
        run = eng.run_match("python", pattern, "", [text])
        assert bool(run.cases[0].matches) is section, (literal, section)


@needs_node
def test_suggest_tests_in_javascript_has_no_trailing_newline_surprise():
    out = server.suggest_tests(r"^[A-Z]{2}\d{3,4}$", "javascript")
    assert "SURPRISES" not in out


def test_suggest_tests_warns_when_unanchored():
    out = server.suggest_tests(r"cat|dog")
    assert "not anchored" in out and "SURPRISES" in out



def test_suggest_tests_unsupported_construct_is_an_explicit_error():
    with pytest.raises(ToolError, match="cannot generate"):
        server.suggest_tests(r"\N{BULLET}")
