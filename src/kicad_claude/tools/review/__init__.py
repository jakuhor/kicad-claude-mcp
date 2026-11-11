"""`review` tool set: design review and release checks."""

from kicad_claude.tools.review import design, release


def register(mcp) -> None:
    design.register(mcp)
    release.register(mcp)
