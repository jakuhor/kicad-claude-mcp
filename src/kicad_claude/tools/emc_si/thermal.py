"""Thermal / current-capacity tools (IPC-2152).

Tools (3):
    calc_trace_current             — amps for a width, or width for a current
    analyze_pcb_current_capacity   — per-track scan of the active PCB
    simulate_thermal_steady_state  — junction temps via a lumped resistive model
"""

from __future__ import annotations

import logging
from typing import Literal

from kicad_claude import state
from kicad_claude.tools.common import on_board
from kicad_claude.adapters import electrical_calc as ec
from kicad_claude.adapters import pcb_editor as pcb_ed
from kicad_claude.adapters import sch_io, thermal_emc

logger = logging.getLogger("kicad-claude.tools.emc_si.thermal")


def register(mcp) -> None:
    @mcp.tool()
    def calc_trace_current(
        width_mm: float | None = None,
        current_a: float | None = None,
        copper_oz: float = 1.0,
        temp_rise_c: float = 10.0,
        location: Literal["external", "internal"] = "external",
        margin_pct: float = 20.0,
    ) -> dict:
        """IPC-2152 trace current: pass `width_mm` for amps, or `current_a` for width.

        The width solve adds `margin_pct` (default 20 %) headroom. `location`:
        external (outer layers, better cooling) or internal. Steady state; derate
        pulse loads.
        """
        if (width_mm is None) == (current_a is None):
            raise ValueError("pass exactly one of width_mm (to get amps) or current_a "
                             "(to solve the width)")
        thickness_mm = copper_oz * 0.035
        cond = dict(copper_thickness_mm=thickness_mm, temp_rise_c=temp_rise_c,
                    location=location)
        out: dict = {"copper_oz": copper_oz, "copper_thickness_mm": thickness_mm,
                     "temp_rise_c": temp_rise_c, "location": location}
        if width_mm is not None:
            amps = ec.trace_current_ipc2152(width_mm, **cond)
            return {"current_a": round(amps, 3), "width_mm": width_mm, **out}
        w = ec.solve_trace_width_for_current(current_a * (1 + margin_pct / 100.0), **cond)
        achieved = ec.trace_current_ipc2152(w, **cond)
        return {"recommended_width_mm": round(w, 4), "achieved_current_a": round(achieved, 3),
                "target_current_a": current_a, "margin_pct": margin_pct, **out}

    @mcp.tool()

    @on_board
    def analyze_pcb_current_capacity(
        copper_oz: float = 1.0,
        temp_rise_c: float = 10.0,
    ) -> dict:
        """Scan every track in the active PCB and report its current capacity.

        Computes per-segment capacity from its width + layer (external for
        F.Cu/B.Cu, internal otherwise). Groups by net so you see the
        weakest link — the narrowest segment dominates.
        """
        tree, _ = (sch_io.parse_file(state.get_active_board_path()),
                   state.get_active_board_path())
        thickness_mm = copper_oz * 0.035
        per_net: dict[str, dict] = {}

        for seg in sch_io.find_children(tree, "segment"):
            width_node = sch_io.find_child(seg, "width")
            layer_node = sch_io.find_child(seg, "layer")
            net_name = pcb_ed.net_of(seg) or "(no net)"
            if not (width_node and layer_node):
                continue
            try:
                width_mm = float(width_node[1])
            except (ValueError, TypeError):
                continue
            layer = layer_node[1] if len(layer_node) >= 2 else ""
            location = "external" if layer in ("F.Cu", "B.Cu") else "internal"

            try:
                amps = ec.trace_current_ipc2152(
                    width_mm,
                    copper_thickness_mm=thickness_mm,
                    temp_rise_c=temp_rise_c,
                    location=location,
                )
            except ValueError:
                continue

            entry = per_net.setdefault(
                net_name,
                {"min_width_mm": float("inf"), "min_capacity_a": float("inf"),
                 "segments": 0, "layers": set()},
            )
            entry["segments"] += 1
            entry["min_width_mm"] = min(entry["min_width_mm"], width_mm)
            entry["min_capacity_a"] = min(entry["min_capacity_a"], amps)
            entry["layers"].add(layer)

        # Format
        result = []
        for net, info in sorted(per_net.items()):
            result.append({
                "net": net,
                "min_width_mm": round(info["min_width_mm"], 4),
                "min_capacity_a": round(info["min_capacity_a"], 3),
                "segment_count": info["segments"],
                "layers": sorted(info["layers"]),
            })
        result.sort(key=lambda r: r["min_capacity_a"])  # weakest first
        return {
            "copper_oz": copper_oz,
            "temp_rise_c": temp_rise_c,
            "total_nets": len(result),
            "weakest_net": result[0] if result else None,
            "nets": result,
        }

    @mcp.tool()
    def simulate_thermal_steady_state(
        components: list,
        ambient_c: float = 25.0,
    ) -> dict:
        """Steady-state junction temperatures. `components`: list of {reference,
        power_w, r_jc_c_per_w (default 1.0), r_ca_c_per_w (default 50)}.
        Returns hottest first, warning near or above 85 C. Typical Rth values
        in help.

        Details:

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
