"""Every child process gets its own stdin, never the server's.

Under stdio the server's stdin is the MCP protocol pipe. A child that
inherits it can block on it or read protocol bytes: KiCAD's bundled Python
running the pcbnew netlist update sat on it until the timeout (OP-18: 260 s
through a stdio session against 3 s in-process, for 55 footprints).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "kicad_claude"
SPAWN = {"run", "Popen", "call", "check_call", "check_output"}


def test_every_subprocess_call_sets_stdin():
    missing = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            f = node.func
            if f.attr in SPAWN and isinstance(f.value, ast.Name) and f.value.id == "subprocess":
                if not any(k.arg in ("stdin", "input") for k in node.keywords):
                    missing.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert missing == [], f"subprocess call without stdin= (use subprocess.DEVNULL): {missing}"
