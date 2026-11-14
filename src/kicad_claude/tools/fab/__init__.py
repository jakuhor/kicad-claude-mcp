"""`fab` tool set: manufacturing exports, panelization."""

from kicad_claude.tools.fab import exports, panelization


def register(mcp) -> None:
    exports.register(mcp)
    panelization.register(mcp)
