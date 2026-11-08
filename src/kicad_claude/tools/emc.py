"""Phase 14 — EMC / EMI heuristic checks.

These are NOT real simulations (which need FEA tools like Ansys / Sonnet).
They are pragmatic checks based on common best-practice rules:

    analyze_ground_coverage  — % of each copper layer occupied by GND zones
    find_long_traces         — antenna candidates (longer = better radiator)
    validate_decoupling_caps — heuristic: any IC pad without a cap within 3 mm
"""

from __future__ import annotations

import logging
import math

from kicad_claude import state
from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import sch_io

logger = logging.getLogger("kicad-claude.tools.emc")


def _parse_net_list(nets: str) -> set[str]:
    return {n.strip() for n in nets.split(",") if n.strip()}


def register(mcp) -> None:
    @mcp.tool()
    def analyze_ground_coverage(ground_nets: str = "") -> dict:
        """Estimate % of each copper layer covered by ground copper.

        Higher is generally better for EMC: a continuous reference plane
        improves signal integrity, reduces radiated emissions, and gives
        return currents a clean path. Aim for >70% on at least one layer
        for any board with high-speed signals.

        `ground_nets`: comma-separated net names to count. Default: every net
        whose name reads as a ground (GND, GND_USB, AGND, PGND, VSS, 0V, ...).

        Area source per layer is reported in `area_source`:
          - `filled` — the zone's filled copper (clearances, thermal reliefs
            and islands already cut out), as last saved. Refill zones in KiCAD
            before trusting it after edits.
          - `outline` — the zone has never been filled, so its outline is
            used: an upper bound, overlapping outlines are double-counted.

        The board area is the outer Edge.Cuts loop (lines + arcs, gr_rect,
        gr_poly or gr_circle) minus inner cutout loops.
        """
        from kicad_claude.adapters import pcb_geometry as geo

        tree = sch_io.parse_file(state.get_active_board_path())
        board_area = geo.board_area_mm2(tree)
        if board_area <= 0:
            return {
                "warning": "no closed Edge.Cuts outline; can't compute coverage",
                "board_area_mm2": 0,
                "by_layer": [],
            }

        wanted = _parse_net_list(ground_nets)

        def _is_gnd(net: str) -> bool:
            return net in wanted if wanted else geo.is_ground_net(net)

        area: dict[str, float] = {}
        nets: dict[str, set[str]] = {}
        sources: dict[str, set[str]] = {}
        for zp in geo.zone_layer_polygons(tree, ed.net_of):
            if not _is_gnd(zp["net"]):
                continue
            layer = zp["layer"]
            area[layer] = area.get(layer, 0.0) + geo.polygon_area(zp["polygon"])
            nets.setdefault(layer, set()).add(zp["net"])
            sources.setdefault(layer, set()).add("filled" if zp["filled"] else "outline")

        report = []
        for layer, a in sorted(area.items()):
            report.append({
                "layer": layer,
                "nets": sorted(nets[layer]),
                "area_source": "+".join(sorted(sources[layer])),
                "zone_area_mm2": round(a, 1),
                "coverage_pct_estimated": round(min(100.0, 100 * a / board_area), 1),
            })
        warnings = []
        copper_layers = ed.get_copper_layer_names(tree)
        for cu in copper_layers:
            if cu not in area:
                warnings.append(f"no ground zone on {cu}")
        if any("outline" in r["area_source"] for r in report):
            warnings.append("some zones are unfilled; their outline area is an upper bound")

        return {
            "board_area_mm2": round(board_area, 1),
            "copper_layers": copper_layers,
            "by_layer": report,
            "warnings": warnings,
        }

    @mcp.tool()
    def find_long_traces(threshold_mm: float = 50.0) -> dict:
        """List nets whose total trace length exceeds `threshold_mm`.

        Long traces are antennas — both for emission (loop area = noise
        radiator) and reception (picking up unwanted noise). For high-speed
        signals (>50 MHz) a length over λ/10 can already cause issues:
        - 100 MHz → λ/10 ≈ 200 mm
        - 1 GHz → λ/10 ≈ 20 mm
        - 2.4 GHz → λ/10 ≈ 8 mm

        Use this list to prioritize where to add ground stitching, shorter
        routing, or impedance control.
        """
        tree = sch_io.parse_file(state.get_active_board_path())
        per_net: dict[str, float] = {}
        for seg in sch_io.find_children(tree, "segment"):
            start = sch_io.find_child(seg, "start")
            end = sch_io.find_child(seg, "end")
            # Copper with no net still radiates; it is reported under one bucket.
            net = ed.net_of(seg) or "(no net)"
            if not (start and end):
                continue
            try:
                length = math.hypot(
                    float(end[1]) - float(start[1]),
                    float(end[2]) - float(start[2]),
                )
            except (ValueError, TypeError):
                continue
            per_net[net] = per_net.get(net, 0.0) + length

        long_nets = [
            {
                "net": net,
                "length_mm": round(L, 2),
                "lambda_estimate_at_1GHz_mm": round(L / 200, 2),  # λ at 1GHz on FR4 ≈ 100 mm, λ/10 ≈ 10
            }
            for net, L in per_net.items()
            if L >= threshold_mm
        ]
        long_nets.sort(key=lambda r: -r["length_mm"])
        return {
            "threshold_mm": threshold_mm,
            "long_nets": long_nets,
            "count": len(long_nets),
        }

    @mcp.tool()
    def validate_decoupling_caps(
        max_distance_mm: float = 3.0,
        ic_reference_prefixes: str = "U,IC",
    ) -> dict:
        """Heuristic: warn about ICs missing a decoupling capacitor nearby.

        For each footprint whose reference starts with one of the given
        prefixes (default U/IC), find the closest capacitor (reference
        starting with C). If the distance exceeds `max_distance_mm`, that
        IC is flagged as potentially missing decoupling.

        Caveat: this is purely geometric — it doesn't check that the cap is
        actually wired to VCC/GND of the IC. It's a starting-point review,
        not a guarantee.
        """
        tree = sch_io.parse_file(state.get_active_board_path())
        prefixes = tuple(p.strip().upper() for p in ic_reference_prefixes.split(",") if p.strip())
        ics: list[tuple[str, float, float]] = []
        caps: list[tuple[str, float, float]] = []
        for fp in ed.iter_footprints(tree):
            ref = ed.get_footprint_reference(fp) or ""
            at = sch_io.find_child(fp, "at")
            if not at or len(at) < 3:
                continue
            x, y = float(at[1]), float(at[2])
            ru = ref.upper()
            if any(ru.startswith(p) for p in prefixes):
                ics.append((ref, x, y))
            elif ru.startswith("C"):
                caps.append((ref, x, y))

        bad: list[dict] = []
        good: list[dict] = []
        for ref, x, y in ics:
            if not caps:
                bad.append({"reference": ref, "nearest_cap": None,
                            "nearest_cap_distance_mm": None})
                continue
            best = min(caps, key=lambda c: math.hypot(c[1] - x, c[2] - y))
            d = math.hypot(best[1] - x, best[2] - y)
            entry = {
                "reference": ref,
                "nearest_cap": best[0],
                "nearest_cap_distance_mm": round(d, 2),
            }
            (good if d <= max_distance_mm else bad).append(entry)
        return {
            "max_distance_mm": max_distance_mm,
            "prefixes": list(prefixes),
            "icas_with_decap": good,
            "icas_missing_decap": bad,
            "ic_count": len(ics),
            "cap_count": len(caps),
        }
