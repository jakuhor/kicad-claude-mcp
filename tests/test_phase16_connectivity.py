"""Phase 16 — schematic connectivity derived from geometry.

Strategy:
- The acceptance gate first: our net partition must equal the one
  `kicad-cli sch export netlist --format kicadxml` reports for a real KiCAD 10
  demo sheet. Hand-written assertions on a toy fixture agree with a broken
  union-find; the cross-check does not.
- Unit tests for the individual connection rules on top of that.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kicad_claude import state
from kicad_claude.adapters import sch_editor as ed
from kicad_claude.adapters import sch_io
from kicad_claude.adapters import sch_netlist as netlist
from kicad_claude.templates.blank import write_blank_project
from kicad_claude.tools import library as lib_tools
from kicad_claude.tools import schematic as sch_tools
from kicad_claude.utils.kicad_paths import find_kicad_cli

FIXTURES = Path(__file__).parent / "fixtures"
KICAD10_FIXTURE = FIXTURES / "kicad10_ecc83-pp_v2.kicad_sch"


# ===== Acceptance gate ===================================================== #


def _partition(nets: dict) -> set[frozenset[tuple[str, str]]]:
    """Reduce our nets to the sets of real pins on each, names ignored.

    Power symbols and power flags are virtual parts; KiCAD leaves them out of
    the netlist, so they are excluded here too.
    """
    out = set()
    for entry in nets.values():
        members = frozenset(
            (p["ref"], p["pin"]) for p in entry["pins"] if not p["power"]
        )
        if members:
            out.add(members)
    return out


def _kicad_partition(xml_path: Path) -> set[frozenset[tuple[str, str]]]:
    out = set()
    for net in ET.parse(xml_path).getroot().find("nets"):
        members = frozenset(
            (n.get("ref"), n.get("pin")) for n in net.findall("node")
        )
        if members:
            out.add(members)
    return out


@pytest.mark.slow
def test_net_partition_matches_kicad_cli(tmp_path):
    """The gate: same nets as KiCAD itself derives, on a real demo sheet."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("no kicad-cli available")

    xml_path = tmp_path / "ref.xml"
    r = subprocess.run(
        [str(cli), "sch", "export", "netlist", "--format", "kicadxml",
         "-o", str(xml_path), str(KICAD10_FIXTURE)],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, f"netlist export failed: {r.stderr}"

    ours = netlist.build_sheet_nets(
        sch_io.parse_file(KICAD10_FIXTURE), KICAD10_FIXTURE
    )
    expected = _kicad_partition(xml_path)
    got = _partition(ours.nets)

    assert got == expected, (
        f"missing={sorted(map(sorted, expected - got))} "
        f"extra={sorted(map(sorted, got - expected))}"
    )


@pytest.mark.slow
def test_labelled_net_names_match_kicad_cli(tmp_path):
    """Names KiCAD took from a label must match ours; generated names may differ."""
    cli = find_kicad_cli()
    if not cli:
        pytest.skip("no kicad-cli available")

    xml_path = tmp_path / "ref.xml"
    subprocess.run(
        [str(cli), "sch", "export", "netlist", "--format", "kicadxml",
         "-o", str(xml_path), str(KICAD10_FIXTURE)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    kicad_named = {
        net.get("name").lstrip("/")
        for net in ET.parse(xml_path).getroot().find("nets")
        if not net.get("name").startswith("Net-")
    }
    ours = netlist.build_sheet_nets(
        sch_io.parse_file(KICAD10_FIXTURE), KICAD10_FIXTURE
    )
    our_named = {n for n in ours.nets if not n.startswith("Net-")}
    assert kicad_named <= our_named, f"missing names: {kicad_named - our_named}"


def test_fixture_net_count_is_stable():
    """Cheap guard so a regression shows up without kicad-cli installed."""
    ours = netlist.build_sheet_nets(
        sch_io.parse_file(KICAD10_FIXTURE), KICAD10_FIXTURE
    )
    assert len(_partition(ours.nets)) == 13


# ===== Connection rules ==================================================== #


@pytest.fixture
def blank_project(tmp_path: Path):
    state.clear_active()
    files = write_blank_project(tmp_path / "net", "net")
    state.set_active(tmp_path / "net", "net")
    yield files
    state.clear_active()


def _tree(files) -> list:
    return sch_io.parse_file(files["sch"])


def _sets(tree: list) -> list[set[tuple[str, str]]]:
    nets = netlist.build_sheet_nets(tree).nets
    return [
        {(p["ref"], p["pin"]) for p in e["pins"]}
        for e in nets.values()
        if e["pins"]
    ]


def _add_r(tree: list, ref: str, x: float, y: float, rotation: float = 0) -> None:
    sym_def = ed.fetch_symbol_def(FIXTURES / "MiniLib.kicad_sym", "Resistor")
    ed.add_symbol(
        tree, qualified_lib_id="MiniLib:Resistor", reference=ref, value="10k",
        x_mm=x, y_mm=y, rotation=rotation, sym_def_node=sym_def, project_name="net",
    )


class TestUnionFind:
    def test_singletons_stay_separate(self):
        uf = netlist._Union()
        uf.add("a")
        uf.add("b")
        assert len(uf.groups()) == 2

    def test_union_is_transitive(self):
        uf = netlist._Union()
        uf.union("a", "b")
        uf.union("b", "c")
        groups = uf.groups()
        assert len(groups) == 1
        assert sorted(next(iter(groups.values()))) == ["a", "b", "c"]


class TestOnSegment:
    def test_point_on_a_horizontal_run(self):
        assert netlist._on_segment((5.0, 0.0), (0.0, 0.0), (10.0, 0.0)) is True

    def test_endpoint_counts_as_on(self):
        assert netlist._on_segment((0.0, 0.0), (0.0, 0.0), (10.0, 0.0)) is True

    def test_point_beyond_the_end_is_off(self):
        assert netlist._on_segment((11.0, 0.0), (0.0, 0.0), (10.0, 0.0)) is False

    def test_point_off_the_line_is_off(self):
        assert netlist._on_segment((5.0, 1.0), (0.0, 0.0), (10.0, 0.0)) is False


class TestConnectionRules:
    def test_two_pins_joined_by_one_wire_form_one_net(self, blank_project):
        tree = _tree(blank_project)
        _add_r(tree, "R1", 50.8, 50.8)
        _add_r(tree, "R2", 76.2, 50.8)
        pins1 = netlist.pins_of_instance(tree, ed.find_symbol_by_reference(tree, "R1"))
        pins2 = netlist.pins_of_instance(tree, ed.find_symbol_by_reference(tree, "R2"))
        a = pins1[0]["point"]
        b = pins2[0]["point"]
        ed.add_wire(tree, a[0], a[1], b[0], b[1])

        sets = _sets(tree)
        assert {("R1", pins1[0]["number"]), ("R2", pins2[0]["number"])} in sets

    def test_crossing_wires_without_a_junction_stay_apart(self, blank_project):
        tree = _tree(blank_project)
        ed.add_wire(tree, 25.4, 50.8, 76.2, 50.8)   # horizontal
        ed.add_wire(tree, 50.8, 25.4, 50.8, 76.2)   # vertical, crosses
        graph = netlist.build_sheet_graph(tree)
        uf = graph["uf"]
        assert uf.find((25.4, 50.8)) != uf.find((50.8, 25.4))

    def test_a_junction_joins_crossing_wires(self, blank_project):
        tree = _tree(blank_project)
        ed.add_wire(tree, 25.4, 50.8, 76.2, 50.8)
        ed.add_wire(tree, 50.8, 25.4, 50.8, 76.2)
        ed.add_junction(tree, 50.8, 50.8)
        graph = netlist.build_sheet_graph(tree)
        uf = graph["uf"]
        assert uf.find((25.4, 50.8)) == uf.find((50.8, 25.4))

    def test_a_t_joint_connects_without_a_junction(self, blank_project):
        """A wire ENDING on another wire is a T — KiCAD connects it."""
        tree = _tree(blank_project)
        ed.add_wire(tree, 25.4, 50.8, 76.2, 50.8)
        ed.add_wire(tree, 50.8, 50.8, 50.8, 76.2)  # ends on the first
        graph = netlist.build_sheet_graph(tree)
        uf = graph["uf"]
        assert uf.find((25.4, 50.8)) == uf.find((50.8, 76.2))

    def test_a_label_names_the_net_it_sits_on(self, blank_project):
        tree = _tree(blank_project)
        _add_r(tree, "R1", 50.8, 50.8)
        pin = netlist.pins_of_instance(
            tree, ed.find_symbol_by_reference(tree, "R1")
        )[0]
        ed.add_wire(tree, pin["point"][0], pin["point"][1], 101.6, pin["point"][1])
        ed.add_label(tree, "VBUS", 101.6, pin["point"][1])

        nets = netlist.build_sheet_nets(tree).nets
        assert "VBUS" in nets
        assert ("R1", pin["number"]) in {
            (p["ref"], p["pin"]) for p in nets["VBUS"]["pins"]
        }

    def test_unlabelled_net_gets_a_generated_name(self, blank_project):
        tree = _tree(blank_project)
        _add_r(tree, "R1", 50.8, 50.8)
        _add_r(tree, "R2", 76.2, 50.8)
        p1 = netlist.pins_of_instance(tree, ed.find_symbol_by_reference(tree, "R1"))[0]
        p2 = netlist.pins_of_instance(tree, ed.find_symbol_by_reference(tree, "R2"))[0]
        ed.add_wire(tree, p1["point"][0], p1["point"][1], p2["point"][0], p2["point"][1])
        nets = netlist.build_sheet_nets(tree).nets
        assert any(n.startswith("Net-(R1-") for n in nets)


class TestMultiUnitPins:
    def test_each_unit_reports_only_its_own_pins(self):
        """U1 in the fixture is a 3-unit valve; the units must not share pins."""
        tree = sch_io.parse_file(KICAD10_FIXTURE)
        per_unit = {}
        for s in ed.iter_instance_symbols(tree):
            if ed.get_symbol_property(s, "Reference") != "U1":
                continue
            unit = sch_io.find_child(s, "unit")[1]
            per_unit[unit] = {p["number"] for p in netlist.pins_of_instance(tree, s)}

        assert len(per_unit) == 3
        all_pins = [p for pins in per_unit.values() for p in pins]
        assert len(all_pins) == len(set(all_pins)), "a pin appears on two units"


class TestDangling:
    def test_a_wire_going_nowhere_is_reported(self, blank_project):
        tree = _tree(blank_project)
        ed.add_wire(tree, 25.4, 50.8, 76.2, 50.8)
        result = netlist.build_sheet_nets(tree)
        assert any(d["kind"] == "wire_end" for d in result.dangling)

    def test_a_no_connect_pin_is_not_reported(self, blank_project):
        tree = _tree(blank_project)
        _add_r(tree, "R1", 50.8, 50.8)
        for pin in netlist.pins_of_instance(
            tree, ed.find_symbol_by_reference(tree, "R1")
        ):
            ed.add_no_connect(tree, pin["point"][0], pin["point"][1])
        result = netlist.build_sheet_nets(tree)
        assert [d for d in result.dangling if d["kind"] == "pin"] == []

    def test_the_kicad_demo_sheet_has_no_dangling_pins(self):
        result = netlist.build_sheet_nets(
            sch_io.parse_file(KICAD10_FIXTURE), KICAD10_FIXTURE
        )
        assert [d for d in result.dangling if d["kind"] == "pin"] == []


# ===== Tool layer ========================================================== #


def _patched_index_with_minilib(tmp_path):
    from tests.test_phase3_schematic import _patched_index_with_minilib as inner
    return inner(tmp_path)


def _make_mcp(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP
    from tests.test_phase3_schematic import _patched_index_with_minilib

    idx = _patched_index_with_minilib(tmp_path)
    monkeypatch.setattr(lib_tools, "load_cache", lambda: idx)
    monkeypatch.setattr(lib_tools, "_index", None)
    mcp = FastMCP("test")
    sch_tools.register(mcp)
    lib_tools.register(mcp)
    return mcp


def _call(mcp, tool_name, /, **kwargs):
    return mcp._tool_manager.get_tool(tool_name).fn(**kwargs)


class TestConnectivityTools:
    def test_list_sch_nets_hides_power_pins_by_default(
        self, blank_project, tmp_path, monkeypatch
    ):
        mcp = _make_mcp(monkeypatch, tmp_path)
        _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1",
              value="10k", x_mm=50.8, y_mm=50.8)
        tree = _tree(blank_project)
        pin = netlist.pins_of_instance(
            tree, ed.find_symbol_by_reference(tree, "R1")
        )[0]
        _call(mcp, "add_wire", x1_mm=pin["point"][0], y1_mm=pin["point"][1],
              x2_mm=pin["point"][0] + 25.4, y2_mm=pin["point"][1])
        _call(mcp, "add_label", net_name="VBUS",
              x_mm=pin["point"][0] + 25.4, y_mm=pin["point"][1])

        res = _call(mcp, "list_sch_nets")
        names = [n["name"] for n in res["nets"]]
        assert "VBUS" in names

    def test_trace_net_unknown_name_raises(
        self, blank_project, tmp_path, monkeypatch
    ):
        mcp = _make_mcp(monkeypatch, tmp_path)
        with pytest.raises(KeyError, match="no net named"):
            _call(mcp, "trace_net", name="NOPE")

    def test_lone_pin_is_named_but_not_connected(
        self, blank_project, tmp_path, monkeypatch
    ):
        """KiCAD names an unreached pin `unconnected-(R1-Pad1)`; read `connected`."""
        mcp = _make_mcp(monkeypatch, tmp_path)
        _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1",
              value="10k", x_mm=50.8, y_mm=50.8)
        res = _call(mcp, "get_pin_net", reference="R1", pin="1")
        assert res["connected"] is False
        assert res["net"].startswith("unconnected-(R1-")

    def test_unknown_reference_has_no_net(
        self, blank_project, tmp_path, monkeypatch
    ):
        mcp = _make_mcp(monkeypatch, tmp_path)
        res = _call(mcp, "get_pin_net", reference="R99", pin="1")
        assert res["net"] is None
        assert res["connected"] is False

    def test_get_pin_net_finds_the_neighbour(
        self, blank_project, tmp_path, monkeypatch
    ):
        mcp = _make_mcp(monkeypatch, tmp_path)
        _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R1",
              value="10k", x_mm=50.8, y_mm=50.8)
        _call(mcp, "add_symbol", lib_id="MiniLib:Resistor", reference="R2",
              value="10k", x_mm=101.6, y_mm=50.8)
        tree = _tree(blank_project)
        p1 = netlist.pins_of_instance(tree, ed.find_symbol_by_reference(tree, "R1"))[0]
        p2 = netlist.pins_of_instance(tree, ed.find_symbol_by_reference(tree, "R2"))[0]
        _call(mcp, "add_wire", x1_mm=p1["point"][0], y1_mm=p1["point"][1],
              x2_mm=p2["point"][0], y2_mm=p2["point"][1])

        res = _call(mcp, "get_pin_net", reference="R1", pin=p1["number"])
        assert res["connected"] is True
        assert ("R2", p2["number"]) in {
            (c["ref"], c["pin"]) for c in res["connected_to"]
        }

    def test_find_dangling_reports_a_lone_wire(
        self, blank_project, tmp_path, monkeypatch
    ):
        mcp = _make_mcp(monkeypatch, tmp_path)
        _call(mcp, "add_wire", x1_mm=25.4, y1_mm=50.8, x2_mm=76.2, y2_mm=50.8)
        res = _call(mcp, "find_dangling")
        assert res["count"] > 0

    def test_bad_scope_rejected(self, blank_project, tmp_path, monkeypatch):
        mcp = _make_mcp(monkeypatch, tmp_path)
        with pytest.raises(ValueError, match="scope must be"):
            _call(mcp, "list_sch_nets", scope="sideways")
