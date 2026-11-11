"""Smoke tests for the server entry point: ping and strict arguments."""

from server import ping


def test_ping_returns_pong():
    assert ping() == "pong"


def test_unknown_keyword_argument_is_rejected():
    """Defects report #2 — a misspelled parameter used to be silently dropped."""
    import asyncio

    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    from server import mcp

    with pytest.raises(ToolError) as excinfo:
        asyncio.run(
            mcp._tool_manager.call_tool("run_drc", {"max_violation": 5})
        )
    message = str(excinfo.value)
    assert "max_violation" in message
    assert "max_violations" in message  # names the accepted spelling


def test_known_arguments_still_pass():
    import asyncio

    from server import mcp

    assert asyncio.run(mcp._tool_manager.call_tool("ping", {})) is not None
