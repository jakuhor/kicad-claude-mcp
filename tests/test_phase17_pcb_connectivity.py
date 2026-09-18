"""Phase 17 — PCB copper connectivity (the ratsnest).

Strategy, same as phase 16: the acceptance gate first. `kicad-cli pcb drc`
reports unconnected items, so KiCAD's own answer is available for any board.
The gate runs against five of KiCAD's demo boards, which between them cover
2-layer and 4-layer stacks, filled zones, thermal relief, inner-layer planes
reached only through vias, and T-joined tracks.

A negative control matters as much as the positive one: a checker that always
says "routed" passes every fully-routed board. `test_cutting_a_net_is_detected`
removes the copper of one net and requires both KiCAD and this module to notice.
"""

from __future__ import annotations

import collections
import json
import subprocess
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import pcb_netlist as pn
from kicad_claude.adapters import sch_io
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import pcb as pcb_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli

DEMOS = Path("C:/Program Files/KiCad/10.0/share/kicad/demos")
DEMO_BOARDS = [
    DEMOS / "ecc83/ecc83-pp_v2.kicad_pcb",
    DEMOS / "interf_u/interf_u.kicad_pcb",
    DEMOS / "complex_hierarchy/complex_hierarchy.kicad_pcb",
    DEMOS / "pic_programmer/pic_programmer.kicad_pcb",
    DEMOS / "video/video.kicad_pcb",
]


def _drc_unconnected(cli: Path, board: Path, out_dir: Path) -> int:
    out = out_dir / (board.stem + ".drc.json")
    subprocess.run(
        [str(cli), "pcb", "drc", "--format", "json", "--severity-all",
         "-o", str(out), str(board)],
        capture_output=True, text=True, timeout=600,
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    return len(report.get("unconnected_items", []))


# ===== Acceptance gate ===================================================== #


@pytest.mark.slow
@pytest.mark.parametrize("board", DEMO_BOARDS, ids=lambda p: p.stem)
def test_unrouted_count_matches_kicad_drc(board, tmp_path):
    """The gate: same unconnected count as KiCAD's own DRC."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("no kicad-cli available")
    if not board.is_file():
        pytest.skip(f"demo board not installed: {board}")

    expected = _drc_unconnected(cli, board, tmp_path)
    ours = pn.list_unrouted(sch_io.parse_file(board))
    assert ours["count"] == expected


@pytest.mark.slow
def test_cutting_a_net_is_detected(tmp_path):
    """Negative control — a checker that always says 'routed' must fail here."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("no kicad-cli available")
    board = DEMOS / "ecc83/ecc83-pp_v2.kicad_pcb"
    if not board.is_file():
        pytest.skip("demo board not installed")

    tree = sch_io.parse_file(board)
    counts = collections.Counter(
        pn._net_of(n)[0] for n in tree[1:] if sch_io.is_call(n, "segment")
    )
    # A net with several segments, but not the biggest — that one is ground.
    victim = counts.most_common(3)[-1][0]
    removed = 0
    for node in list(tree[1:]):
        if sch_io.is_call(node, "segment") and pn._net_of(node)[0] == victim:
            tree.remove(node)
            removed += 1
    assert removed > 0

    broken = tmp_path / "broken.kicad_pcb"
    sch_io.write_file(broken, tree)

    ours = pn.list_unrouted(sch_io.parse_file(broken))
    assert ours["count"] > 0
    assert ours["count"] == _drc_unconnected(cli, broken, tmp_path)


# ===== Pad geometry ======================================================== #


class TestPadTransform:
    def test_unrotated_pad_is_offset_from_the_footprint_origin(self):
        assert pn.pad_to_board_xy(100.0, 50.0, 0, 2.0, 1.0) == (102.0, 51.0)

    def test_ninety_degrees_rotates_in_kicad_y_down_sense(self):
        # (x, y) -> (y, -x) about the footprint origin.
        assert pn.pad_to_board_xy(100.0, 50.0, 90, 2.0, 0.0) == (100.0, 48.0)

    def test_one_eighty_negates_both(self):
        assert pn.pad_to_board_xy(100.0, 50.0, 180, 2.0, 1.0) == (98.0, 49.0)

    @pytest.mark.slow
    def test_rotated_pads_land_on_their_own_copper(self):
        """The check that picked this sign convention in the first place."""
        board = DEMOS / "interf_u/interf_u.kicad_pcb"
        if not board.is_file():
            pytest.skip("demo board not installed")
        tree = sch_io.parse_file(board)
        tracks = pn._tracks(tree)
        near = 0
        total = 0
        for pad in pn.list_pads(tree):
            if pad["net"] == 0:
                continue
            same = [t for t in tracks if t["net"] == pad["net"]]
            if not same:
                continue
            total += 1
            if any(pn._touches(pad, t) for t in same):
                near += 1
        assert total > 100
        assert near / total > 0.95


class TestPadListing:
    def test_pads_carry_net_and_layers(self):
        board = DEMOS / "ecc83/ecc83-pp_v2.kicad_pcb"
        if not board.is_file():
            pytest.skip("demo board not installed")
        pads = pn.list_pads(sch_io.parse_file(board))
        assert pads
        through = [p for p in pads if p["type"] == "thru_hole"]
        assert through, "the ecc83 demo is a through-hole board"
        assert "F.Cu" in through[0]["layers"] and "B.Cu" in through[0]["layers"]

    def test_find_pad_by_reference(self):
        board = DEMOS / "ecc83/ecc83-pp_v2.kicad_pcb"
        if not board.is_file():
            pytest.skip("demo board not installed")
        tree = sch_io.parse_file(board)
        any_pad = pn.list_pads(tree)[0]
        found = pn.find_pad(tree, any_pad["ref"], any_pad["pad"])
        assert found is not None
        assert found["point"] == any_pad["point"]

    def test_find_pad_returns_none_when_absent(self):
        board = DEMOS / "ecc83/ecc83-pp_v2.kicad_pcb"
        if not board.is_file():
            pytest.skip("demo board not installed")
        assert pn.find_pad(sch_io.parse_file(board), "NOPE99", "1") is None


class TestGeometryHelpers:
    def test_point_segment_distance_perpendicular(self):
        assert pn._point_segment_distance((5.0, 2.0), (0.0, 0.0), (10.0, 0.0)) == 2.0

    def test_point_segment_distance_past_the_end(self):
        assert pn._point_segment_distance((13.0, 0.0), (0.0, 0.0), (10.0, 0.0)) == 3.0

    def test_point_in_polygon(self):
        square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        assert pn._point_in_polygon((5.0, 5.0), square) is True
        assert pn._point_in_polygon((15.0, 5.0), square) is False

    def test_distance_to_polygon_from_inside(self):
        square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        assert pn._distance_to_polygon((5.0, 5.0), square) == 5.0

    def test_zone_reaches_a_thermally_relieved_pad(self):
        """The pad centre sits in the pour's hole; spokes still connect it."""
        # A square pour with a square hole punched around the pad.
        ring = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]
        pad = {"point": (10.0, 10.0), "radius_mm": 0.5}
        assert pn._zone_touches_pad(pad, ring) is True
        far = {"point": (40.0, 40.0), "radius_mm": 0.5}
        assert pn._zone_touches_pad(far, ring) is False


class TestUnfilledZones:
    def test_a_zone_without_fill_data_is_reported(self, tmp_path):
        tree = [
            sch_io.sym("kicad_pcb"),
            [sch_io.sym("version"), 20241229],
            [
                sch_io.sym("zone"),
                [sch_io.sym("net"), 1, "GND"],
                [sch_io.sym("layer"), "F.Cu"],
            ],
        ]
        result = pn.list_unrouted(tree)
        assert result["zones_filled"] is False
        assert result["unfilled_zones"] == 1


# ===== Tool layer ========================================================== #


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    write_blank_project(tmp_path / "rat", "rat")
    state.set_active(tmp_path / "rat", "rat")
    yield tmp_path / "rat"
    state.clear_active()


def _make_mcp():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test")
    pcb_tools.register(mcp)
    lib_tools.register(mcp)
    return mcp


def _call(mcp, tool_name, /, **kwargs):
    return mcp._tool_manager.get_tool(tool_name).fn(**kwargs)


class TestConnectivityTools:
    def test_empty_board_has_nothing_unrouted(self, blank_project):
        res = _call(_make_mcp(), "list_unrouted")
        assert res["count"] == 0

    def test_list_pads_on_an_empty_board(self, blank_project):
        assert _call(_make_mcp(), "list_pads") == []

    def test_get_pad_position_unknown_reference(self, blank_project):
        with pytest.raises(KeyError, match="no pad"):
            _call(_make_mcp(), "get_pad_position", reference="U1", pad="1")

    def test_net_route_status_unknown_net(self, blank_project):
        with pytest.raises(KeyError, match="no net named"):
            _call(_make_mcp(), "net_route_status", net_name="GND")

    def test_list_pads_unknown_reference(self, blank_project):
        with pytest.raises(KeyError, match="no footprint"):
            _call(_make_mcp(), "list_pads", reference="U99")


@pytest.mark.slow
class TestOnARealBoard:
    """Tool behaviour against a demo board copied into the active project."""

    @pytest.fixture
    def demo_project(self, tmp_path):
        board = DEMOS / "ecc83/ecc83-pp_v2.kicad_pcb"
        if not board.is_file():
            pytest.skip("demo board not installed")
        state.clear_active()
        files = write_blank_project(tmp_path / "demo", "demo")
        Path(files["pcb"]).write_bytes(board.read_bytes())
        state.set_active(tmp_path / "demo", "demo")
        yield files
        state.clear_active()

    def test_routed_board_reports_nothing_unrouted(self, demo_project):
        assert _call(_make_mcp(), "list_unrouted")["count"] == 0

    def test_net_route_status_says_routed(self, demo_project):
        res = _call(_make_mcp(), "net_route_status", net_name="GND")
        assert res["routed"] is True
        assert len(res["connected_groups"]) == 1
        assert res["pads"] > 1

    def test_get_pad_position_returns_the_net(self, demo_project):
        mcp = _make_mcp()
        pads = _call(mcp, "list_pads")
        first = next(p for p in pads if p["net"])
        res = _call(mcp, "get_pad_position",
                    reference=first["reference"], pad=first["pad"])
        assert res["position_mm"] == first["position_mm"]
        assert res["net"] == first["net"]
