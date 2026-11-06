"""MCP stdio smoke test: spawns the real server over stdio, checks the tool list, and checks
that instructive errors survive the trip to the agent (MCPServer hides any exception that
is not a ToolError)."""

import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

EXPECTED_TOOLS = {
    "test_regex",
    "explain_regex",
    "check_backtracking",
    "compare_engines",
    "convert_pattern",
    "replace_preview",
    "suggest_tests",
}


def params() -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=["-m", "regexlab_mcp"])


@pytest.mark.anyio
async def test_tools_and_a_session_over_stdio():
    async with stdio_client(params()) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "regexlab"
        tools = (await session.list_tools()).tools
        assert {t.name for t in tools} == EXPECTED_TOOLS
        assert all(t.description and len(t.description) > 80 for t in tools)

        res = await session.call_tool(
            "test_regex",
            {"pattern": r"(?P<y>\d{4})", "subject": ["in 2024", "none"], "engines": ["python"]},
        )
        assert not res.is_error
        text = res.content[0].text
        assert "[3:7] '2024'" in text and "named: y='2024'" in text

        res = await session.call_tool("explain_regex", {"pattern": r"^a+$"})
        assert not res.is_error and "one or more times" in res.content[0].text

        res = await session.call_tool(
            "check_backtracking", {"pattern": r"(a+)+$", "timeout_s": 2.0}
        )
        assert not res.is_error and "RISK: HIGH" in res.content[0].text


@pytest.mark.anyio
async def test_instructive_errors_reach_the_agent():
    async with stdio_client(params()) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("explain_regex", {"pattern": "a(b"})
        assert res.is_error and "missing )" in res.content[0].text and "^" in res.content[0].text
        res = await session.call_tool("test_regex", {"pattern": "a", "subject": "a", "flags": "Q"})
        assert res.is_error and "unknown flag" in res.content[0].text
        res = await session.call_tool("test_regex", {"pattern": "a", "subject": "a",
                                                    "engines": ["perl"]})
        assert res.is_error and "unknown engine" in res.content[0].text


@pytest.fixture
def anyio_backend():
    return "asyncio"
