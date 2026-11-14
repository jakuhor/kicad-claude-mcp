"""`pcb` tool set: board edit and query, routing, RF."""

from kicad_claude.tools.pcb import layout, routing, rf


def register(mcp) -> None:
    layout.register(mcp)
    routing.register(mcp)
    rf.register(mcp)
