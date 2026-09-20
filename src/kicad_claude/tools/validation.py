"""Phase 7 — validation tools (ERC / DRC).

Tools:
    run_erc   — Electrical Rules Check on the active schematic
    run_drc   — Design Rules Check on the active PCB

Both shell out to `kicad-cli` and return structured JSON (errors,
warnings, violations with positions). Raw report JSON is also written next
to the source file so it can be inspected by humans.
"""

from __future__ import annotations

import logging

from kicad_claude import state
from kicad_claude.adapters import kicad_cli

logger = logging.getLogger("kicad-claude.tools.validation")


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
    """Register Phase 7 tools on the FastMCP instance."""

    @mcp.tool()
    def run_erc(severity: str = "all", timeout_seconds: float = 60.0) -> dict:
        """Run KiCAD's Electrical Rules Check on the active schematic.

        Args:
            severity: 'all', 'error', 'warning', or 'exclusions'. Maps to
                `kicad-cli sch erc --severity-<value>`.
            timeout_seconds: hard cap on the kicad-cli invocation.

        Returns counts by severity, the list of violations with positions,
        and the path to the raw JSON report.
        """
        proj = state.get_active()
        return kicad_cli.run_erc(
            proj.sch_path, severity=severity, timeout=timeout_seconds
        )

    @mcp.tool()
    def run_drc(
        severity: str = "all",
        schematic_parity: bool = True,
        all_track_errors: bool = False,
        refill_zones: bool = False,
        timeout_seconds: float = 120.0,
        summary: bool = False,
        max_violations: int = 0,
    ) -> dict:
        """Run KiCAD's Design Rules Check on the active PCB.

        Args:
            severity: 'all', 'error', 'warning', or 'exclusions'.
            schematic_parity: include parity check between PCB and schematic.
            all_track_errors: report each individual track error (more verbose).
            refill_zones: refill zones before validation (use after add_zone /
                add_ground_plane). Saves the board with refilled zones.
            timeout_seconds: hard cap.
            summary: return only the counts — `violations_by_type`,
                `unconnected_items_by_type` and `schematic_parity_by_type`
                give one row per (type, severity) — and no individual
                entries. The full report is always on disk at `raw_path`.
            max_violations: keep at most this many entries per list (0 = all);
                `<list>_omitted` says how many were dropped.

        The full lists run to hundreds of kilobytes on a mid-size unrouted
        board, mostly ratsnest lines, which is past the tool-result limit —
        call with `summary=True` first, then drill in.

        Returns counts, violations, unconnected items, parity findings, and
        the path to the raw JSON report.
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
