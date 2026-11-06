"""check_backtracking: static detection plus an empirical probe that must not hang CI."""

import time

import pytest

from helpers import needs_311, needs_node
from regexlab_mcp import backtrack, server
from regexlab_mcp import engines as eng
from regexlab_mcp import rxparse as rx
from regexlab_mcp.charsets import Cx

BUDGET = 2.0
SLACK = 25.0  # generous: shared CI runners are slow; the point is "does not hang"


def kinds(pattern: str, dialect: str = "python") -> list[str]:
    parsed = rx.parse(pattern, dialect)
    return [h.kind for h in backtrack.static_scan(parsed, Cx(dialect, parsed.flags))]


@pytest.mark.parametrize("pattern", [
    r"(a+)+$", r"(a*)*b", r"(.*)*x", r"^(\w+\s?)*$", r"^(\d+)*$", r"(x+x+)+y",
    r"([a-z]+)*\d", r"(a|a)*b", r"(a|aa)+b", r"(\w|\d)+$",
])
def test_exponential_shapes_are_detected_statically(pattern):
    found = kinds(pattern)
    assert found and found[0] in ("nested", "alternation"), found


@pytest.mark.parametrize("pattern,kind", [
    (r".*.*=.*", "adjacent"),
    (r"\d+\d+$", "adjacent"),
    (r"\s+$", "leading"),
])
def test_polynomial_shapes_are_detected_statically(pattern, kind):
    assert kind in kinds(pattern)


@pytest.mark.parametrize("pattern", [
    r"^\d+,\d+$", r"^(\d+,)+x$", r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$",
    r"^(a|b)*c$", r"^\w+\s\w+$", r"^(?:foo|bar)+$", r"^[^,]*,[^,]*$",
])
def test_safe_shapes_are_not_flagged_exponential(pattern):
    found = kinds(pattern)
    assert not [k for k in found if k in ("nested", "alternation")], found


@needs_311
def test_possessive_and_atomic_remove_the_hotspot():
    assert "nested" not in kinds(r"(?>a+)+$")
    assert "nested" not in kinds(r"(a++)+$")
    assert "nested" in kinds(r"(a+)+$")


def test_bounded_small_repeat_is_not_a_hotspot():
    assert kinds(r"(a+){1,3}$") == []


def test_known_redos_pattern_is_confirmed_in_python_within_the_timeout():
    t0 = time.perf_counter()
    out = server.check_backtracking(r"(a+)+$", "python", timeout_s=BUDGET)
    assert time.perf_counter() - t0 < SLACK
    assert "RISK: HIGH" in out
    assert "[nested]" in out and "exponential" in out
    assert "slow input: 'a' * " in out


@needs_node
def test_known_redos_pattern_is_confirmed_in_javascript_within_the_timeout():
    t0 = time.perf_counter()
    out = server.check_backtracking(r"^(\w+\s?)*$", "javascript", timeout_s=BUDGET)
    assert time.perf_counter() - t0 < SLACK
    assert "RISK: HIGH" in out and "javascript:" in out


@needs_node
def test_engine_all_probes_every_available_engine():
    out = server.check_backtracking(r"(a|aa)+b", "all", timeout_s=BUDGET)
    assert "python" in out and "javascript" in out
    assert "RISK: HIGH" in out


def test_quadratic_pattern_is_measured_as_polynomial():
    out = server.check_backtracking(r"\s+$", "python", timeout_s=BUDGET)
    assert "RISK: MEDIUM" in out and "n^" in out
    assert "slow input: ' ' * " in out


def test_safe_pattern_reports_no_finding_and_is_fast():
    t0 = time.perf_counter()
    out = server.check_backtracking(r"^\d{3}-\d{4}$", "python", timeout_s=BUDGET)
    assert time.perf_counter() - t0 < SLACK
    assert "RISK: none found" in out and "not a proof" in out


def test_static_suspicion_not_reproduced_is_reported_as_unconfirmed():
    # alternation with a shared first character but no real ambiguity: flagged, then cleared
    out = server.check_backtracking(r"^(?:foo|food)+$", "python", timeout_s=BUDGET)
    assert "[alternation]" in out
    assert "RISK: LOW/UNCONFIRMED" in out


def test_probe_data_points_are_real_timings():
    parsed = rx.parse(r"(a+)+$")
    cx = Cx("python", "")
    spot = backtrack.static_scan(parsed, cx)[0]
    verdicts = backtrack.probe_hotspot(parsed, cx, spot, ["python"], "", 2.0)
    assert verdicts[0].level == "exponential"
    assert verdicts[0].seconds < SLACK


def test_invalid_pattern_is_an_error_not_a_probe():
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="missing"):
        server.check_backtracking("(a+", "python")


def test_asking_for_an_unavailable_engine_says_so(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setattr(eng, "node_path", lambda: None)
    with pytest.raises(ToolError, match="node"):
        server.check_backtracking("a+", "javascript")


def test_node_absent_is_noted_not_fatal_for_all(monkeypatch):
    monkeypatch.setattr(eng, "node_path", lambda: None)
    out = server.check_backtracking(r"^\d+$", "all", timeout_s=BUDGET)
    assert "javascript: skipped" in out
