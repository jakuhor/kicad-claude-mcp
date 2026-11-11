"""Validation tools (ERC / DRC).

Tools:
    run_erc   — Electrical Rules Check on the active schematic
    run_drc   — Design Rules Check on the active PCB

Both shell out to `kicad-cli` and return structured JSON (errors,
warnings, violations with positions). Raw report JSON is also written next
to the source file so it can be inspected by humans.
"""

from __future__ import annotations

import logging
from typing import Literal

from kicad_claude import state
from kicad_claude.tools.common import on_board
from kicad_claude.adapters import kicad_cli

logger = logging.getLogger("kicad-claude.tools.core.validation")

Severity = Literal["all", "error", "warning", "exclusions"]


# Lists a DRC report carries; each one can run to thousands of entries on an
# unrouted mid-size board, which is past what a tool result can return.
_DRC_LISTS = ("violations", "unconnected_items", "schematic_parity")


def _counts_by_type(entries: list[dict]) -> list[dict]:
    """[{type, severity, count}] for a list of shaped violations, biggest first."""
    counts: dict[tuple[str, str], int] = {}
    for e in entries:
        key = (e.get("type", ""), e.get("severity", ""))
        counts[key] = counts.get(key, 0) + 1
    return sorted(
        [{"type": t, "severity": sev, "count": n} for (t, sev), n in counts.items()],
        key=lambda d: (-d["count"], d["type"]),
    )


def _condense_drc(report: dict, summary: bool, max_violations: int) -> dict:
    """Replace the report's long lists with counts (`summary`) or a cap."""
    out = dict(report)
    for key in _DRC_LISTS:
        entries = out.get(key) or []
        out[f"{key}_by_type"] = _counts_by_type(entries)
        if summary:
            out[key] = []
            out[f"{key}_omitted"] = len(entries)
        elif max_violations and len(entries) > max_violations:
            out[key] = entries[:max_violations]
            out[f"{key}_omitted"] = len(entries) - max_violations
    return out


def register(mcp) -> None:
    """Register the tools."""

    @mcp.tool()
    def run_erc(severity: Severity = "all", timeout_seconds: float = 60.0) -> dict:
        """KiCAD ERC on the schematic: counts, violations with positions, raw JSON path."""
        proj = state.get_active()
        return kicad_cli.run_erc(
            proj.sch_path, severity=severity, timeout=timeout_seconds
        )

    @mcp.tool()

    @on_board
    def run_drc(
        severity: Severity = "all",
        schematic_parity: bool = True,
        all_track_errors: bool = False,
        refill_zones: bool = False,
        timeout_seconds: float = 120.0,
        summary: bool = False,
        max_violations: int = 0,
    ) -> dict:
        """KiCAD DRC on the active board: counts, violations, unconnected, parity.

        Full lists can exceed the result limit: call `summary=True` (counts per
        type only) first, then cap with `max_violations` (0 = all). Full report
        stays at `raw_path`. `refill_zones` refills and saves the board.
        """
        report = kicad_cli.run_drc(
            state.get_active_board_path(),
            severity=severity,
            schematic_parity=schematic_parity,
            all_track_errors=all_track_errors,
            refill_zones=refill_zones,
            timeout=timeout_seconds,
        )
        return _condense_drc(report, summary=summary, max_violations=max_violations)
