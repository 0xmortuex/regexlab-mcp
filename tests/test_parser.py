"""The syntax tree parser: structure, spans, errors and dialect rules."""

import pytest

from regexlab_mcp import rxparse as rx


def test_structure_and_spans():
    r = rx.parse(r"^(?P<y>\d{4})-[a-c]+$")
    assert r.group_count == 1 and r.group_names == {"y": 1}
    items = r.root.branches[0].items
    assert [type(i).__name__ for i in items] == ["Anchor", "Group", "Char", "Quant", "Anchor"]
    group = items[1]
    assert isinstance(group, rx.Group) and group.kind == "named"
    assert r.pattern[group.start:group.header_end] == "(?P<y>"
    assert r.pattern[group.start:group.end] == r"(?P<y>\d{4})"
    q = items[3]
    assert isinstance(q, rx.Quant) and (q.min, q.max, q.mode) == (1, None, "greedy")
    assert r.pattern[q.start:q.end] == "[a-c]+"


@pytest.mark.parametrize("pattern,mn,mx,mode", [
    ("a*", 0, None, "greedy"), ("a+?", 1, None, "lazy"), ("a??", 0, 1, "lazy"),
    ("a{3}", 3, 3, "greedy"), ("a{2,}", 2, None, "greedy"), ("a{2,5}?", 2, 5, "lazy"),
    ("a{,4}", 0, 4, "greedy"),
])
def test_quantifier_forms_python(pattern, mn, mx, mode):
    q = rx.parse(pattern).root.branches[0].items[0]
    assert isinstance(q, rx.Quant) and (q.min, q.max, q.mode) == (mn, mx, mode)


def test_possessive_and_atomic_are_flagged_per_dialect():
    py = rx.parse("a*+(?>b)", "python")
    assert [i.level for i in py.issues] == ["warn", "warn"]
    js = rx.parse("a*+(?>b)", "javascript")
    assert [i.level for i in js.issues] == ["error", "error"]
    assert rx.parse("a*+", "regex").issues == []


def test_brace_literal_rules_differ_by_dialect():
    py = rx.parse("a{,3}", "python").root.branches[0].items[0]
    assert isinstance(py, rx.Quant) and py.max == 3
    js = rx.parse("a{,3}", "javascript").root.branches[0].items
    assert all(isinstance(i, rx.Char) for i in js) and len(js) == 5  # literal text
    assert isinstance(rx.parse("a{}", "python").root.branches[0].items[1], rx.Char)


def test_named_group_syntax_per_dialect():
    assert [i.level for i in rx.parse("(?<n>a)", "python").issues] == ["error"]
    assert [i.level for i in rx.parse("(?P<n>a)", "javascript").issues] == ["error"]
    assert rx.parse("(?<n>a)", "javascript").issues == []
    assert rx.parse("(?<n>a)\\k<n>", "javascript").group_names == {"n": 1}
    assert rx.parse("(?P<n>a)(?P=n)", "python").group_names == {"n": 1}


def test_lookbehind_width_issue_in_python_only():
    assert rx.parse("(?<=ab)c", "python").issues == []
    assert [i.level for i in rx.parse("(?<=a+)c", "python").issues] == ["error"]
    assert rx.parse("(?<=a+)c", "javascript").issues == []
    assert rx.parse("(?<=a+)c", "regex").issues == []


def test_anchors_escapes_per_dialect():
    py = rx.parse(r"\Aab\Z", "python").root.branches[0].items
    assert isinstance(py[0], rx.Anchor) and py[0].kind == "abs_start"
    js = rx.parse(r"\Aab\Z", "javascript")
    assert all(not isinstance(i, rx.Anchor) for i in js.root.branches[0].items)
    assert len(js.issues) == 2 and all(i.level == "warn" for i in js.issues)


def test_inline_flags_global_and_scoped():
    r = rx.parse("(?i)a(?s:b.)")
    assert "i" in r.flags
    groups = [n for n in rx.walk(r.root) if isinstance(n, rx.Group)]
    assert groups[0].kind == "flags" and groups[0].flags_on == "s"
    late = rx.parse("a(?i)b", "python")
    assert any(i.level == "error" for i in late.issues)


def test_verbose_mode_records_ignored_spans():
    r = rx.parse("a b  # comment\n c", "python", "x")
    assert [type(i).__name__ for i in r.root.branches[0].items] == ["Char", "Char", "Char"]
    assert len(r.ignored) >= 3


def test_character_class_items():
    cls = rx.parse(r"[^a-z\d_\-]").root.branches[0].items[0]
    assert isinstance(cls, rx.CharClass) and cls.negated
    assert [type(i).__name__ for i in cls.items] == ["Range", "ClassEsc", "Char", "Char"]
    first_close = rx.parse("[]a]", "python").root.branches[0].items[0]
    assert isinstance(first_close, rx.CharClass) and len(first_close.items) == 2
    empty = rx.parse("[]", "javascript").root.branches[0].items[0]
    assert isinstance(empty, rx.CharClass) and empty.items == []


def test_unsupported_constructs_become_nodes_not_guesses():
    r = rx.parse(r"(?(1)a|b)(?R)\N{BULLET}", "regex")
    uns = [n for n in rx.walk(r.root) if isinstance(n, rx.Unsupported)]
    assert len(uns) == 3


@pytest.mark.parametrize("pattern,msg,pos", [
    ("(abc", "missing )", 0),
    ("abc)", "unbalanced", 3),
    ("*a", "nothing to repeat", 0),
    ("a**", "multiple repeat", 2),
    ("a{3,1}", "min repeat greater than max", 1),
    ("[z-a]", "bad character range", 1),
    ("[abc", "unterminated character set", 0),
    ("\\", "bad escape", 0),
    (r"\q", "bad escape", 0),
    (r"(a)\2", "invalid group reference", 3),
    ("(?P<n>a)(?P<n>b)", "redefinition of group name", 8),
    ("(?^)", "unknown extension", 0),
    ("^*", "nothing to repeat", 1),
])
def test_syntax_errors_have_message_and_position(pattern, msg, pos):
    with pytest.raises(rx.ParseError) as ei:
        rx.parse(pattern)
    assert msg in ei.value.reason and ei.value.pos == pos
    assert "^" in ei.value.detail()


def test_width_computation():
    assert rx.width(rx.parse("abc").root) == (3, 3)
    assert rx.width(rx.parse("a|bcd").root) == (1, 3)
    assert rx.width(rx.parse("a+").root) == (1, None)
    assert rx.width(rx.parse("(?:ab){2,3}").root) == (4, 6)


def test_js_unicode_escapes_need_the_u_flag():
    r = rx.parse(r"\u{1F600}", "javascript", "u")
    item = r.root.branches[0].items[0]
    assert isinstance(item, rx.Char) and item.value == "\U0001F600"
    r2 = rx.parse(r"\p{L}", "javascript")
    assert r2.issues and r2.issues[0].level == "warn"
    assert isinstance(rx.parse(r"\p{L}", "javascript", "u").root.branches[0].items[0], rx.Prop)
    with pytest.raises(rx.ParseError):
        rx.parse(r"\p{L}", "python")
