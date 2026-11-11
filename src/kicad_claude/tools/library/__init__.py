"""`library` tool set: library index and search, symbol / footprint creation."""

from kicad_claude.tools.library import index, create


def register(mcp) -> None:
    index.register(mcp)
    create.register(mcp)
