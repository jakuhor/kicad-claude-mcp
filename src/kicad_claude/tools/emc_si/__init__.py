"""`emc_si` tool set: EMC, signal integrity, thermal."""

from kicad_claude.tools.emc_si import emc, signal_integrity, thermal


def register(mcp) -> None:
    emc.register(mcp)
    signal_integrity.register(mcp)
    thermal.register(mcp)
