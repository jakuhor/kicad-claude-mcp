"""Phase 15 — pseudo-FEM thermal and EMC analysis tools.

Tools (3):
    simulate_thermal_steady_state    — junction temps via lumped resistive model
    estimate_crosstalk               — closed-form NEXT/FEXT for parallel microstrips
    check_return_path_continuity     — flag signal traces with poor GND reference

These are NOT real FEM solvers. For production hardware that ships at
volume, validate with Ansys Icepak / Sonnet / OpenEMS. Our outputs are
ballpark estimates good for early-stage layout decisions.
"""

from __future__ import annotations

import logging
import re

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import sch_io, thermal_emc

logger = logging.getLogger("kicad-claude.tools.simulation")

# Obvious power rails, skipped by default in the return-path check:
# +5V, -18V, +3V3, VCC, VDD, VBUS, VIN, VBAT. Matched against the last
# sheet-path component, so `/400_symetric_analog/+20V` counts.
_POWER_NET_RE = re.compile(r"^([+-]\d|\+?V(CC|DD|BUS|IN|BAT)(\b|_))", re.I)


def register(mcp) -> None:
    @mcp.tool()
    def simulate_thermal_steady_state(
        components: list,
        ambient_c: float = 25.0,
    ) -> dict:
        """Compute steady-state junction temperatures for components dissipating power.

        Each entry in `components` is a dict with:
            reference     : str (e.g. "U1", "Q1")
            power_w       : float, dissipation in W
            r_jc_c_per_w  : float (default 1.0) — junction-to-case Rth
            r_ca_c_per_w  : float (default 50)  — case-to-ambient Rth (no heatsink)

        Common Rth values:
            SMD SOIC-8 free air:    ~120 °C/W
            SMD SOIC-8 on copper:   ~50-80 °C/W
            TO-220 free air:        ~62 °C/W
            TO-220 on heatsink:     ~5-10 °C/W
            QFN/DFN:                ~30-50 °C/W

        Returns sorted list (hottest first) with junction temp + warning when
        approaching/exceeding 85 °C (commercial grade limit).
        """
        results = thermal_emc.solve_thermal_network(list(components), ambient_c=ambient_c)
        # Aggregate stats
        max_t = max((r["junction_temp_c"] for r in results), default=ambient_c)
        total_power = sum(float(c.get("power_w", 0)) for c in components)
        return {
            "ambient_c": ambient_c,
            "component_count": len(results),
            "total_dissipation_w": round(total_power, 3),
            "max_junction_temp_c": round(max_t, 2),
            "components": results,
        }

    @mcp.tool()
    def estimate_crosstalk(
        parallel_length_mm: float,
        separation_mm: float,
        dielectric_height_mm: float = 0.21,
        rise_time_ns: float = 1.0,
        dielectric_constant: float = 4.5,
    ) -> dict:
        """Closed-form NEXT/FEXT estimate for two parallel microstrips.

        Inputs:
            parallel_length_mm:  length over which the two traces run side by side
            separation_mm:       center-to-center (or edge-to-edge) gap
            dielectric_height_mm: height of the trace above its reference plane
            rise_time_ns:        signal rise time (faster = more crosstalk)
            dielectric_constant: εᵣ (FR4 ≈ 4.5; Rogers RO4350B = 3.66)

        Output ratios (0–1) are the fraction of the aggressor amplitude that
        couples onto the victim line. Multiply by the aggressor V_pp to get
        induced voltage. Rule of thumb: keep NEXT < 5% (−26 dB) for general
        digital, < 1% (−40 dB) for sensitive analog/RF.
        """
        return thermal_emc.estimate_crosstalk_coupling(
            parallel_length_mm=parallel_length_mm,
            separation_mm=separation_mm,
            dielectric_height_mm=dielectric_height_mm,
            rise_time_ns=rise_time_ns,
            er=dielectric_constant,
        )

    @mcp.tool()
    def check_return_path_continuity(
        signal_nets: str = "",
        ground_nets: str = "",
        max_segments_to_check: int = 200,
        adjacent_only: bool = True,
        end_clearance_mm: float = 0.6,
    ) -> dict:
        """Find signal traces that don't have a ground reference plane underneath.

        For each track on the active PCB, 11 points along it are tested
        against the ground copper on the reference layer(s). Segments with
        <80% coverage are flagged — the return current has nowhere clean to
        flow there, which causes EMI and impedance discontinuities.

        Ground copper is each zone's **filled** polygon per layer (KiCAD 10
        `(net "GND") (layers ...)` multi-layer zones included), so antipads,
        clearances and splits count as voids. A never-filled zone falls back to
        its outline (`filled: false` in `gnd_zones`) and over-reports coverage.

        `signal_nets`: comma-separated nets to check (default: every net that
        is neither a ground nor an obvious power rail).
        `ground_nets`: comma-separated reference nets (default: every net whose
        name reads as a ground — GND, GND_USB, AGND, PGND, VSS, 0V, ...).
        `adjacent_only`: test only the copper layer(s) directly above/below the
        trace in the stackup (default). False accepts ground on any other layer.
        `end_clearance_mm`: ignore this much of each segment end — tracks end
        on vias, whose antipad in the plane below is a hole by design. Segments
        shorter than twice this are not judged (`segments_too_short`).
        `max_segments_to_check` caps the total segment count.
        """
        from kicad_claude.adapters import pcb_geometry as geo

        tree = sch_io.parse_file(state.get_active_board_path())
        wanted_gnd = {n.strip() for n in ground_nets.split(",") if n.strip()}

        def _is_gnd(net: str) -> bool:
            return net in wanted_gnd if wanted_gnd else geo.is_ground_net(net)

        gnd_zones = [zp for zp in geo.zone_layer_polygons(tree, pcb_ed.net_of)
                     if _is_gnd(zp["net"])]

        net_filter = {n.strip() for n in signal_nets.split(",") if n.strip()}

        segments: list[dict] = []
        for seg in sch_io.find_children(tree, "segment"):
            start = sch_io.find_child(seg, "start")
            end = sch_io.find_child(seg, "end")
            layer = sch_io.find_child(seg, "layer")
            net_name = pcb_ed.net_of(seg, tree)
            if not (net_name and start and end and layer):
                continue
            if net_filter:
                if net_name not in net_filter:
                    continue
            elif _is_gnd(net_name) or _POWER_NET_RE.search(net_name.rsplit("/", 1)[-1]):
                continue
            segments.append({
                "net": net_name,
                "layer": str(layer[1]),
                "start": (float(start[1]), float(start[2])),
                "end": (float(end[1]), float(end[2])),
            })
            if len(segments) >= max_segments_to_check:
                break

        reference_layers = None
        copper = pcb_ed.get_copper_layer_names(tree)
        if adjacent_only and copper:
            reference_layers = {
                name: [copper[j] for j in (i - 1, i + 1) if 0 <= j < len(copper)]
                for i, name in enumerate(copper)
            }

        findings = thermal_emc.check_return_path(
            segments, gnd_zones, reference_layers=reference_layers,
            end_clearance_mm=end_clearance_mm)
        too_short = sum(
            1 for s in segments
            if ((s["end"][0] - s["start"][0]) ** 2
                + (s["end"][1] - s["start"][1]) ** 2) ** 0.5 <= 2 * end_clearance_mm)

        summary: dict[tuple, dict] = {}
        for zp in gnd_zones:
            e = summary.setdefault((zp["net"], zp["layer"], zp["filled"]), {
                "net": zp["net"], "layer": zp["layer"], "filled": zp["filled"],
                "polygons": 0, "area_mm2": 0.0})
            e["polygons"] += 1
            e["area_mm2"] += geo.polygon_area(zp["polygon"])
        for e in summary.values():
            e["area_mm2"] = round(e["area_mm2"], 1)

        result = {
            "segments_checked": len(segments),
            "segments_too_short": too_short,
            "reference_rule": "adjacent copper layer" if reference_layers else "any other layer",
            "gnd_zones": sorted(summary.values(), key=lambda e: (e["layer"], e["net"])),
            "findings": findings,
            "issue_count": len(findings),
            "tip": "Run add_ground_plane on a back layer to fix most return-path issues.",
        }
        if not gnd_zones:
            result["warning"] = ("no ground zone found, so every segment is flagged. "
                                 "Pass ground_nets= if the ground net has an unusual name.")
        return result
