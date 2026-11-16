"""`sch` tool set: schematic edit and query, annotation, PCB update."""

from kicad_claude.tools.sch import batch, layout, schematic, sync


def register(mcp) -> None:
    schematic.register(mcp)
    batch.register(mcp)
    layout.register(mcp)
    sync.register(mcp)
