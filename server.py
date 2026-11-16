"""KiCAD-Claude MCP server entry point.

Starts with the `core` tool set; the model loads the others with
`load_toolset` (see `kicad_claude/toolsets.py`). `KICAD_MCP_TOOLSETS` picks
the startup sets (`all` to register everything).
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env early so adapters can read API credentials at import/use time.
load_dotenv(Path(__file__).parent / ".env")

from kicad_claude import toolsets  # noqa: E402
from kicad_claude.strict_args import install as install_strict_args  # noqa: E402
from kicad_claude.utils.logging import setup_logging  # noqa: E402

logger = setup_logging()

mcp = FastMCP("kicad-claude", instructions=(
    "Start with core; add tool groups with load_toolset. "
    + toolsets.SELECT_BY_NAME
    + " help(tool) gives the full docs of any tool."))


@mcp.tool()
def ping() -> str:
    """Health check. Returns 'pong' if the server is reachable."""
    logger.info("ping called")
    return "pong"


toolset_manager = toolsets.install(mcp)

# The active project survives a restart (`/mcp` reconnect), per working dir.
from kicad_claude import state  # noqa: E402
from kicad_claude.utils.kicad_paths import cache_dir  # noqa: E402

state.enable_session(cache_dir() / "session.json")
if (restored := state.restore()) is not None:
    logger.info("restored active project %s", restored.name)

# Unknown keyword arguments must fail loudly, not be dropped (defect 2).
install_strict_args(mcp)


if __name__ == "__main__":
    logger.info("starting kicad-claude MCP server (stdio)")
    mcp.run()
