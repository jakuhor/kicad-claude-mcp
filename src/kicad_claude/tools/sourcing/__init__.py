"""`sourcing` tool set: distributors, vendor parts, BOM."""

from kicad_claude.tools.sourcing import distributors, bom


def register(mcp) -> None:
    distributors.register(mcp)
    bom.register(mcp)
