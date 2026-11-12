"""Signal-integrity tools (`emc_si` set).

    calc_impedance               — Z₀ / Z_diff of a geometry, or the width for a target
    estimate_crosstalk           — closed-form NEXT / FEXT for parallel microstrips
    check_return_path_continuity — flag tracks with poor ground reference beneath

Closed-form estimates for early layout decisions, not field-solver sign-off.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from kicad_claude import state
from kicad_claude.tools.common import on_board
from kicad_claude.arg_types import clean
from kicad_claude.adapters import electrical_calc as ec
from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import sch_io, thermal_emc

logger = logging.getLogger("kicad-claude.tools.emc_si.signal_integrity")

# Obvious power rails, skipped by default in the return-path check:
# +5V, -18V, +3V3, VCC, VDD, VBUS, VIN, VBAT. Matched against the last
# sheet-path component, so `/400_symetric_analog/+20V` counts.
_POWER_NET_RE = re.compile(r"^([+-]\d|\+?V(CC|DD|BUS|IN|BAT)(\b|_))", re.I)


def register(mcp) -> None:
    @mcp.tool()
    def calc_impedance(
        kind: Literal["microstrip", "stripline", "differential", "cpwg"],
        dielectric_height_mm: float,
        width_mm: float | None = None,
        target_ohms: float | None = None,
        gap_mm: float | None = None,
        dielectric_constant: float = 4.5,
        copper_thickness_mm: float = 0.035,
    ) -> dict:
        """Trace impedance: pass `width_mm` for Z, or `target_ohms` to solve the width.

        `dielectric_height_mm`: to the reference plane (stripline: full
        plane-to-plane distance). `gap_mm`: pair gap (`differential`) or gap to
        the ground pour (`cpwg`). Defaults FR4 εr 4.5, 1 oz Cu. Closed-form
        estimates, not field-solver sign-off.

        Details:

        Formulas: microstrip and stripline IPC-2141A (microstrip valid for
        0.1 ≤ w/h ≤ 2.0), differential = edge-coupled microstrip (Polar
        approximation), cpwg = coplanar waveguide with a ground plane below.
        `differential` also returns the single-ended Z₀ of one trace.

        Common targets: USB 2.0 90 Ω diff; Ethernet, HDMI, MIPI 100 Ω diff;
        PCIe Gen3+ 85 Ω diff; RF 50 Ω single; video 75 Ω single.

        Solving bisects the width over 0.05 – 10 mm and fails when the target
        is outside what that range reaches.
        """
        if (width_mm is None) == (target_ohms is None):
            raise ValueError("pass exactly one of width_mm (to get Z) or target_ohms "
                             "(to solve the width)")
        common = dict(gap_mm=gap_mm, er=dielectric_constant,
                      thickness_mm=copper_thickness_mm)
        solved = width_mm is None
        if solved:
            width_mm = ec.solve_width_for_impedance(
                kind, target_ohms, dielectric_height_mm, **common)
        z = ec.impedance(kind, width_mm, dielectric_height_mm, **common)
        out: dict = {
            "kind": kind,
            "impedance_ohms": round(z, 2),
            "width_mm": round(width_mm, 4),
            "geometry": {
                "dielectric_height_mm": dielectric_height_mm,
                "gap_mm": gap_mm,
                "dielectric_constant": dielectric_constant,
                "copper_thickness_mm": copper_thickness_mm,
            },
        }
        if solved:
            out["target_ohms"] = target_ohms
            out["error_ohms"] = round(z - target_ohms, 3)
        if kind == "differential":
            out["single_ended_ohms"] = round(ec.microstrip_impedance(
                width_mm, dielectric_height_mm, er=dielectric_constant,
                thickness_mm=copper_thickness_mm), 2)
        if kind == "microstrip":
            out["ratio_w_over_h"] = round(width_mm / dielectric_height_mm, 3)
        return out

    @mcp.tool()
    def estimate_crosstalk(
        parallel_length_mm: float,
        separation_mm: float,
        dielectric_height_mm: float = 0.21,
        rise_time_ns: float = 1.0,
        dielectric_constant: float = 4.5,
    ) -> dict:
        """Closed-form NEXT / FEXT ratios (fraction of the aggressor amplitude)
        for two parallel microstrips from `parallel_length_mm`,
        `separation_mm`, `dielectric_height_mm`, `rise_time_ns` and
        `dielectric_constant`. Keep NEXT < 5 % for digital, < 1 % for sensitive
        analog / RF.

        Details:

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


    @on_board
    def check_return_path_continuity(
        signal_nets: list[str] | None = None,
        ground_nets: list[str] | None = None,
        max_segments_to_check: int = 200,
        adjacent_only: bool = True,
        end_clearance_mm: float = 0.6,
    ) -> dict:
        """Flag track segments with < 80 % ground copper beneath (filled zones;
        never-filled zones fall back to their outline). `signal_nets` /
        `ground_nets`: net lists (defaults: non-power nets / ground-named
        nets); `adjacent_only` (default) checks only neighbouring layers;
        `end_clearance_mm` ignores via ends; `max_segments_to_check` caps the
        work. For the whole board, per net, use analyze_emc (EM-REF-GAP).

        Details:

        For each track on the active PCB, 11 points along it are tested
        against the ground copper on the reference layer(s). Segments with
        <80% coverage are flagged — the return current has nowhere clean to
        flow there, which causes EMI and impedance discontinuities.

        Ground copper is each zone's **filled** polygon per layer (KiCAD 10
        `(net "GND") (layers ...)` multi-layer zones included), so antipads,
        clearances and splits count as voids. A never-filled zone falls back to
        its outline (`filled: false` in `gnd_zones`) and over-reports coverage.

        `signal_nets`: nets to check (default: every net that
        is neither a ground nor an obvious power rail).
        `ground_nets`: reference nets (default: every net whose
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
        wanted_gnd = set(clean(ground_nets))

        def _is_gnd(net: str) -> bool:
            return net in wanted_gnd if wanted_gnd else geo.is_ground_net(net)

        gnd_zones = [zp for zp in geo.zone_layer_polygons(tree, pcb_ed.net_of)
                     if _is_gnd(zp["net"])]

        net_filter = set(clean(signal_nets))

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
