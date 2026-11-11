"""`core` tool set: project, open/create/state, component list, ERC/DRC, lock recovery."""

from kicad_claude.tools.core import project, validation


def register(mcp) -> None:
    project.register(mcp)
    validation.register(mcp)
