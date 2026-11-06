"""explain_regex: token-by-token text, dialect notes, honesty about unsupported syntax."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from regexlab_mcp import server


def test_explains_every_token_of_an_email_pattern():
    out = server.explain_regex(r"^(?P<user>[\w.+-]+)@(?:[a-z\d-]+\.)+[a-z]{2,}$")
    assert 'start of capture group 1, named "user"' in out
    assert "any one of: a word character" in out
    assert "one or more times (greedy)" in out
    assert "2 or more times" in out
    assert "non-capturing group" in out
    assert "repeat the group above one or more times" in out
    assert "Capture groups: 1" in out


def test_lazy_possessive_lookaround_backref_and_flags():
    out = server.explain_regex(r"(?<=\$)(\d+?)(?!x)\1(?i:z)++", "regex")
    assert "positive lookbehind" in out
    assert "negative lookahead" in out
    assert "lazy" in out and "possessive" in out
    assert "the same text that capture group 1 matched" in out
    assert "inline flags: ignore case" in out


def test_dialect_changes_the_description():
    py = server.explain_regex(r"\d$", "python")
    js = server.explain_regex(r"\d$", "javascript")
    assert "Unicode" in py and "Python quirk" in py
    assert "ASCII only" in js and "Python quirk" not in js and "does NOT count" in js


def test_flags_change_the_description():
    assert "start of a line" in server.explain_regex("^a", flags="m")
    assert "including newlines" in server.explain_regex("a.", flags="s")
    assert "[A-Za-z0-9_] (ASCII flag)" in server.explain_regex(r"\w", flags="a")


def test_engine_specific_errors_are_flagged():
    out = server.explain_regex("(?<n>a)", "python")
    assert "ERROR (python)" in out and "(?P<name>" in out


def test_unsupported_constructs_are_marked_not_guessed():
    out = server.explain_regex(r"(?(1)a|b)(?R)", "regex")
    assert out.count("NOT EXPLAINED") == 2
    assert "Not explained:" in out


def test_invalid_pattern_is_a_tool_error_with_caret():
    with pytest.raises(ToolError) as ei:
        server.explain_regex("a(b")
    msg = str(ei.value)
    assert "missing )" in msg and "a(b" in msg and "^" in msg


def test_unknown_engine_is_a_tool_error():
    with pytest.raises(ToolError, match="unknown engine"):
        server.explain_regex("a", "perl")


def test_group_summary_lists_names():
    out = server.explain_regex(r"(?P<a>x)(y)")
    assert '1 ("a") = (?P<a>x)' in out and "2 = (y)" in out
