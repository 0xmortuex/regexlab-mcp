"""Exact expected matches in each real engine, and the places engines genuinely differ."""

import pytest

from helpers import HAS_NODE, HAS_REGEX, needs_node, needs_regex
from regexlab_mcp import engines as eng

ALL = ["python"] + (["javascript"] if HAS_NODE else []) + (["regex"] if HAS_REGEX else [])


def spans(engine: str, pattern: str, subject: str, flags: str = "") -> list[tuple[int, int, str]]:
    run = eng.run_match(engine, pattern, flags, [subject])
    assert run.error is None, run.error
    return [(m.start, m.end, m.text) for m in run.cases[0].matches]


def named(engine: str) -> str:
    return r"(?<y>\d{4})-(\d\d)" if engine == "javascript" else r"(?P<y>\d{4})-(\d\d)"


@pytest.mark.parametrize("engine", ALL)
def test_all_matches_with_groups_and_named_groups(engine):
    run = eng.run_match(engine, named(engine), "", ["on 2024-05 and 2025-06."])
    assert run.error is None
    ms = run.cases[0].matches
    assert [(m.start, m.end, m.text) for m in ms] == [(3, 10, "2024-05"), (15, 22, "2025-06")]
    assert [(g.start, g.end, g.text) for g in ms[0].groups if g] == [(3, 7, "2024"), (8, 10, "05")]
    assert ms[1].named == {"y": "2025"}


@pytest.mark.parametrize("engine", ALL)
def test_empty_matches_are_reported_identically(engine):
    assert spans(engine, "a*", "baac") == [(0, 0, ""), (1, 3, "aa"), (3, 3, ""), (4, 4, "")]


@pytest.mark.parametrize("engine", ALL)
def test_unmatched_group_is_none(engine):
    run = eng.run_match(engine, r"(a)|(b)", "", ["b"])
    m = run.cases[0].matches[0]
    assert m.groups[0] is None and m.groups[1] is not None and m.groups[1].text == "b"


@pytest.mark.parametrize("engine", ALL)
def test_spans_are_code_points_not_utf16_units(engine):
    # the emoji is one code point (two UTF-16 units); the span of "a" must be [1:2] everywhere
    assert spans(engine, "a", "\U0001F600a") == [(1, 2, "a")]


@pytest.mark.parametrize("engine", ALL)
def test_flags_i_m_s(engine):
    assert spans(engine, "^b", "a\nb", "m") == [(2, 3, "b")]
    assert spans(engine, "ABC", "xabc", "i") == [(1, 4, "abc")]
    assert spans(engine, "a.b", "a\nb", "s") == [(0, 3, "a\nb")]
    assert spans(engine, "a.b", "a\nb") == []


@pytest.mark.parametrize("engine", ALL)
def test_syntax_error_is_reported_not_raised(engine):
    run = eng.run_match(engine, "(unclosed", "", ["x"])
    assert run.error and run.error_kind == "compile"
    assert run.cases == []


@pytest.mark.parametrize("engine", ALL)
def test_flag_error_names_the_flag(engine):
    flag = "y" if engine != "javascript" else "x"
    run = eng.run_match(engine, "a", flag, ["a"])
    assert run.error_kind == "flag" and repr(flag) in run.error


def test_python_named_group_syntax_is_rejected_by_javascript_and_vice_versa():
    run = eng.run_match("python", "(?<y>a)", "", ["a"])
    assert run.error_kind == "compile"
    if HAS_NODE:
        js = eng.run_match("javascript", "(?P<y>a)", "", ["a"])
        assert js.error_kind == "compile"


@needs_node
def test_dollar_and_trailing_newline_differ_python_vs_javascript():
    assert spans("python", "^abc$", "abc\n") == [(0, 3, "abc")]
    assert spans("javascript", "^abc$", "abc\n") == []
    assert spans("python", r"^abc\Z", "abc\n") == []


@needs_node
def test_unicode_digits_and_word_chars_differ():
    arabic = "٣٤"
    assert spans("python", r"\d+", arabic) == [(0, 2, arabic)]
    assert spans("javascript", r"\d+", arabic) == []
    assert spans("python", r"\w+", "café") == [(0, 4, "café")]
    assert spans("javascript", r"\w+", "café") == [(0, 3, "caf")]
    assert spans("javascript", r"\w+", "café", "u") == [(0, 3, "caf")]  # u does not widen \w


@needs_node
def test_lookbehind_width_python_vs_javascript():
    assert eng.run_match("python", "(?<=a+)b", "", ["aab"]).error_kind == "compile"
    assert spans("javascript", "(?<=a+)b", "aab") == [(2, 3, "b")]


@needs_node
def test_backreference_to_unset_group_differs():
    assert spans("python", r"(a)?\1", "b") == []
    assert spans("javascript", r"(a)?\1", "b") == [(0, 0, ""), (1, 1, "")]


@needs_node
def test_dot_and_astral_characters():
    assert spans("python", ".", "\U0001F600") == [(0, 1, "\U0001F600")]
    js = spans("javascript", ".", "\U0001F600")
    assert len(js) == 2  # two UTF-16 halves without the u flag
    assert spans("javascript", ".", "\U0001F600", "u") == [(0, 1, "\U0001F600")]


# -- Turkish dotless i and friends ---------------------------------------------------


@needs_node
def test_turkish_i_case_insensitive_python_vs_javascript():
    # JavaScript /i never maps non-ASCII U+0130 / U+0131 onto ASCII i / I
    js = spans("javascript", "i", "İ ı I i", "i")
    assert [t for _, _, t in js] == ["I", "i"]
    # Python re folds the dotless/dotted pair onto i as well
    py = spans("python", "i", "İ ı I i", "i")
    assert "ı" in [t for _, _, t in py] and "I" in [t for _, _, t in py]


@needs_node
def test_dotless_i_pattern_does_not_match_ascii_in_javascript():
    assert [t for _, _, t in spans("javascript", "ı", "I i ı", "i")] == ["ı"]


@needs_node
def test_kelvin_sign_needs_the_u_flag_in_javascript():
    kelvin = "K"
    assert spans("python", "k", kelvin, "i") == [(0, 1, kelvin)]
    assert spans("javascript", "k", kelvin, "i") == []
    assert spans("javascript", "k", kelvin, "iu") == [(0, 1, kelvin)]


@needs_regex
def test_regex_module_runs_and_reports_its_version():
    run = eng.run_match("regex", r"\p{Lu}+", "", ["abc DEF"])
    assert run.error is None and run.version
    assert [m.text for m in run.cases[0].matches] == ["DEF"]


@needs_regex
def test_regex_module_supports_variable_lookbehind():
    assert spans("regex", "(?<=a+)b", "aab") == [(2, 3, "b")]


def test_availability_reports_reason_for_missing_engines():
    avail = eng.availability()
    assert avail["python"] is None
    for name in ("javascript", "regex"):
        assert avail[name] is None or "not" in avail[name]


def test_requesting_an_unavailable_engine_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(eng, "node_path", lambda: None)
    with pytest.raises(eng.EngineUnavailable, match="node"):
        eng.pick_engines(["javascript"])
    engines, notes = eng.pick_engines(None)
    assert "javascript" not in engines and any("javascript: skipped" in n for n in notes)


def test_unknown_engine_name_lists_choices():
    with pytest.raises(ValueError, match="unknown engine"):
        eng.pick_engines(["perl"])
