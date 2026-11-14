"""`sch` tool set: schematic edit and query, annotation, PCB update."""

from kicad_claude.tools.sch import schematic, sync


def register(mcp) -> None:
    schematic.register(mcp)
    sync.register(mcp)
