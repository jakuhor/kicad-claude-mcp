"""Smoke test for the Phase 0 ping tool."""

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
            mcp._tool_manager.call_tool("set_design_rules", {"min_clearance": 0.15})
        )
    message = str(excinfo.value)
    assert "min_clearance" in message
    assert "min_clearance_mm" in message  # names the accepted spelling


def test_known_arguments_still_pass():
    import asyncio

    from server import mcp

    assert asyncio.run(mcp._tool_manager.call_tool("ping", {})) is not None
