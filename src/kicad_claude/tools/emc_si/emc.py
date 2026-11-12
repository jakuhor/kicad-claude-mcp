"""EMC / EMI heuristic checks.

These are NOT real simulations (which need FEA tools like Ansys / Sonnet).
They are pragmatic checks based on common best-practice rules:

    analyze_ground_coverage  — % of each copper layer occupied by GND zones
    find_long_traces         — antenna candidates (longer = better radiator)
    validate_decoupling_caps — heuristic: any IC pad without a cap within 3 mm
    analyze_emc              — EMC pre-compliance review on the filled copper
                               (ground & stackup, switching converters, …)
"""

from __future__ import annotations

import logging
from typing import Literal
import math

from kicad_claude import state
from kicad_claude.tools.common import on_board
from kicad_claude.arg_types import EmcGroup, clean
from kicad_claude.adapters import pcb_editor as ed
from kicad_claude.adapters import sch_io

logger = logging.getLogger("kicad-claude.tools.emc_si.emc")


def register(mcp) -> None:
    @mcp.tool()
    @on_board
    def analyze_ground_coverage(ground_nets: list[str] | None = None) -> dict:
        """Percent of each copper layer covered by ground copper (aim > 70 % on
        one layer for high-speed boards). `ground_nets`: default
        every ground-named net. Uses filled zone copper; a never-filled zone
        falls back to its outline (`area_source`), so refill zones first.

        Details:

        Higher is generally better for EMC: a continuous reference plane
        improves signal integrity, reduces radiated emissions, and gives
        return currents a clean path. Aim for >70% on at least one layer
        for any board with high-speed signals.

        `ground_nets`: net names to count. Default: every net
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

        wanted = set(clean(ground_nets))

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

    @on_board
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

    @on_board
    def validate_decoupling_caps(
        max_distance_mm: float = 3.0,
        ic_reference_prefixes: list[str] | None = None,
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
        prefixes = tuple(p.upper() for p in clean(ic_reference_prefixes, ("U", "IC")))
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

    @mcp.tool()

    @on_board
    def analyze_emc(
        groups: list[EmcGroup] | None = None,
        standard: Literal["fcc-class-b", "fcc-class-a", "cispr-class-b", "cispr-class-a"] = "fcc-class-b",
        fsw: dict[str, float] | None = None,
        iout: dict[str, float] | None = None,
        rail_voltages: dict[str, float] | None = None,
        f_max_hz: float = 200e6,
        stitch_spacing_mm: float = 0.0,
        rise_time_s: float = 10e-9,
        decap_max_mm: float = 3.0,
        cap_via_max_mm: float = 1.5,
        pdn: dict | None = None,
        edge_keepout_mm: float = 1.0,
        esd_max_mm: float = 10.0,
        diff_skew_ps: float = 25.0,
        resolution_mm: float = 0.5,
        harmonic_limits: dict | None = None,
        netlist_path: str = "",
    ) -> dict:
        """EMC pre-compliance review of the active board's filled copper (refill
        first). `groups` (default all): ground, switching, decoupling, io,
        thermal; `EM-*` IDs. Pass `fsw={"U1": 500e3}`, optionally `iout`,
        `rail_voltages`, `pdn`, `harmonic_limits` (your limit profile).
        Returns `test_plan`. First-pass estimator.

        Details:

        Groups (`groups`, default all):
          - `ground` — layer roles from fill (an inner GND layer is a plane,
            not a signal layer), adjacent reference plane for every routed
            layer (EM-STACK-REF), floating ground islands (EM-GND-ISLAND),
            ground nets and the parts joining them (EM-GND-DOMAINS), signals
            crossing a change of reference-plane net without a bridging cap
            (EM-SPLIT-CROSS), signal tracks with no copper on their reference
            layer under them - nearest adjacent poured layer, both within 2x
            dielectric; via / through-hole antipads skipped (EM-REF-GAP, one
            per net >= 1 mm; warning for clock / high-speed or a 5 mm
            stretch; `reference_gaps`), signal and supply vias whose layer change moves
            the return current to another plane (each layer's nearest adjacent
            pour; both only within 2x dielectric): same plane net needs a via
            of that net reaching both planes within 2x the first dielectric,
            at least 1 mm; different plane nets need a capacitor between them
            within 5 mm (EM-VIA-STITCH, one per net; warning for clock /
            high-speed signals; `via_transitions`), ground-via
            stitching vs λ/20 at `f_max_hz`
            (EM-STITCH), ground at the board edge (EM-EDGE-GND), connector
            ground pins (EM-CONN-GND);
          - `switching` — per switching regulator (from the schematic and the
            datasheet specs): switch-node copper area (EM-SW-AREA), input hot
            loop through the real pads (EM-SW-CIN), harmonic envelope and a
            differential-mode radiation estimate against `standard`
            (`fcc-class-b|fcc-class-a|cispr-class-b|cispr-class-a`; EM-SW-RAD),
            switching inductors near the edge or a connector;
          - `decoupling` — every IC supply pin (power_in, from the netlist)
            to its nearest capacitor on the same net (EM-DEC-DIST >
            `decap_max_mm`, EM-DEC-NONE; one finding per IC and net), each cap's ground pad to the nearest
            ground via (EM-DEC-VIA > `cap_via_max_mm`), per-rail capacitor bank
            vs a target impedance `pdn={"+3V3": 0.05}` or
            `pdn={"+3V3": {"ripple": 0.03, "i_step": 0.5}}` (EM-PDN; ESL / ESR
            only from extracted cap datasheets — never assumed); every bank
            also gets `z_profile_ohm` at 100 kHz / 1 / 10 / 100 MHz (series
            C-ESR-L per cap, L = datasheet ESL + layout mounting inductance; no
            plane capacitance, unknown ESR = 0, as `z_profile_basis` states);
          - `io` — every connector line: TVS / ESD diode present and its
            distance (EM-IO-ESD, EM-IO-ESD-DIST > `esd_max_mm`), series filter
            on signal lines (EM-IO-FILTER; both one finding per connector,
            lines in `nets`); signal tracks within
            `edge_keepout_mm` of the edge (EM-EDGE-TRACE); clocks near
            connectors (EM-CLK-CONN); crystal-to-IC distance (EM-XTAL-DIST);
            diff-pair skew (EM-DIFF-SKEW > `diff_skew_ps`); clock / high-speed
            tracks parallel to another net closer than 3H (EM-3H); pair layer
            changes (EM-DIFF-LAYERS) and reference-plane continuity under a
            pair (EM-DIFF-REF); connector aperture slot resonance (c / 2L,
            L from the pad extent) vs the switching harmonics
            (EM-SHIELD-SLOT) and the shielding the radiation estimates leave
            to an enclosure (EM-SHIELD-SE);
          - `thermal` — class-II MLCCs at >= 50 % of their `Voltage` rating
            (EM-TH-BIAS; the capacitance loss only from an extracted datasheet,
            never a generic table) and ferrites within 10 mm of a switching
            regulator (EM-TH-FERRITE).

        Always returns `test_plan`: tests to run for `standard`, the
        switchers' harmonics in the conducted and radiated bands, near-field
        probe points (switch nodes, connectors) and the top risks.

        `harmonic_limits` (a screen against your own limits; none are
        shipped): {"name": "where the limits come from", "unit": "dBuV/m"
        (with "distance_m") | "dBuA", "bands": [{"name", "f_start_hz",
        "f_stop_hz", "limit_db", "limit_stop_db" (optional: sloped in log
        f)}]}. Each switcher's trapezoid harmonic envelope (the radiation
        estimate's: Ott E at distance_m, or the harmonic current) gives the
        worst margin per band in `harmonic_screen` (`screening_only: true`);
        a negative margin is EM-HARM-LIMIT. An invalid profile is an error.

        `fsw={"U1": 500e3, "U2/ch1": 1.8e6}` — switching frequencies. The
        datasheet value is used only when the schematic meets its stated
        condition (RT pin floating, or one resistor to ground of the stated
        value within 1 %, with any SYNC / CLKIN pin idle; `fsw_source` says
        which); otherwise it is a hint. `iout` — load currents (default: the datasheet
        IOUT max, worst case). `rail_voltages` — unregulated inputs, as for
        `analyze_power_tree`. Results are first-pass estimators, not sign-off.
        """
        from kicad_claude.adapters import emc_review, power_review
        from kicad_claude.tools.common import datasheet_values, project_netlist, specs_by_ref

        wanted = tuple(clean(groups, emc_review.GROUPS))
        bad = [g for g in wanted if g not in emc_review.GROUPS]
        if bad:
            raise ValueError(f"unknown group(s) {bad}; valid: {list(emc_review.GROUPS)}")
        tree = sch_io.parse_file(state.get_active_board_path())
        power = nl = None
        specs: dict = {}
        if {"switching", "decoupling", "thermal"} & set(wanted):
            nl = power_review.parse_netlist(project_netlist(netlist_path))
            specs = specs_by_ref(nl)
            power = power_review.analyze(nl, datasheet=datasheet_values(nl),
                                         rail_voltages=rail_voltages or {})
        result = emc_review.analyze(
            tree, wanted, f_max_hz=f_max_hz, stitch_spacing_mm=stitch_spacing_mm or None,
            resolution_mm=resolution_mm, power=power, nl=nl, specs=specs, fsw=fsw or {},
            iout=iout or {}, standard=standard, rise_time_s=rise_time_s,
            decap_max_mm=decap_max_mm, cap_via_max_mm=cap_via_max_mm, pdn=pdn or {},
            edge_keepout_mm=edge_keepout_mm, esd_max_mm=esd_max_mm, diff_skew_ps=diff_skew_ps,
            harmonic_limits=harmonic_limits)
        result["note"] = ("First-pass EMC estimator on the board file; confirm with "
                          "measurement or a field solver before sign-off.")
        return result
