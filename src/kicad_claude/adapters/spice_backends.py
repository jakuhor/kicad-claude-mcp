"""SPICE simulator backends: ngspice and LTspice.

Both take a plain deck (`.ac` / `.op` / `.tran` statements, `.end`) and
return the AC response of the asked nodes in dB. ngspice gets a `.control`
block that writes the data; LTspice runs the deck in batch mode (`-b`) and
its binary `.raw` file is read (UTF-16 header, complex double data for AC,
float64 time + float32 traces for transient unless `Flags: double`).

Discovery: ngspice from PATH. LTspice from `LTSPICE_PATH`, then absolute
PATH entries (a relative entry such as "." could resolve into the project
folder, so it is never searched), then the default install folders. The
binary is always launched by its full path, in a temporary folder.

Process handling follows kicad-happy 3.0.1 (MIT, © 2025 Andrew Klofas):
spice/scripts/spice_simulator.py:_run_process_tree / _kill_process_tree.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

SIMULATORS = ("auto", "ngspice", "ltspice")
LTSPICE_NAMES = ("LTspice.exe", "XVIIx64.exe", "ltspice", "LTspice")


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        subprocess.run([os.path.join(root, "System32", "taskkill.exe"), "/F", "/T", "/PID",
                        str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    else:
        try:
            os.killpg(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass


def run_tree(cmd: list[str], cwd: Path, timeout: float) -> int:
    """Run `cmd` in its own process group with output to the null device;
    on timeout (or any error) kill the whole tree and re-raise."""
    iso = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
           else {"start_new_session": True})
    with subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, **iso) as proc:
        try:
            return proc.wait(timeout=timeout)
        except BaseException:
            _kill_tree(proc.pid)
            proc.kill()
            proc.wait()
            raise


# ---- .raw reader ---------------------------------------------------------------- #


def read_raw(path: Path) -> dict:
    """LTspice binary .raw: {"plot", "flags", "variables": [names],
    "data": {name: [values]}} - complex values for AC, floats otherwise."""
    blob = Path(path).read_bytes()
    enc = "utf-16-le" if blob[1:2] == b"\x00" else "latin-1"
    marker = "Binary:\n".encode(enc)
    i = blob.find(marker)
    if i < 0:
        raise ValueError(f"{path}: no binary section (ASCII .raw is not supported)")
    header = blob[:i].decode(enc)
    meta, names, in_vars = {}, [], False
    for line in header.splitlines():
        if in_vars:
            parts = line.split()
            if len(parts) >= 3 and parts[0].isdigit():
                names.append(parts[1])
            continue
        if line.startswith("Variables:"):
            in_vars = True
            continue
        k, _, v = line.partition(":")
        meta[k.strip().lower()] = v.strip()
    flags = meta.get("flags", "").lower().split()
    nvar, npts = int(meta["no. variables"]), int(meta["no. points"])
    body = blob[i + len(marker):]
    data = {n: [] for n in names}
    off = 0
    if "complex" in flags:
        step = 16 * nvar
        for _ in range(npts):
            vals = struct.unpack_from(f"<{2 * nvar}d", body, off)
            off += step
            for k, n in enumerate(names):
                data[n].append(complex(vals[2 * k], vals[2 * k + 1]))
    else:
        dbl = "double" in flags
        fmt = "<d" + ("d" if dbl else "f") * (nvar - 1)
        step = struct.calcsize(fmt)
        for _ in range(npts):
            vals = struct.unpack_from(fmt, body, off)
            off += step
            for k, n in enumerate(names):
                data[n].append(abs(vals[k]) if k == 0 else vals[k])  # time sign = compression flag
    return {"plot": meta.get("plotname", ""), "flags": flags, "variables": names, "data": data}


def _db(z: complex) -> float:
    return 20 * math.log10(max(abs(z), 1e-300))


# ---- backends -------------------------------------------------------------------- #


class Backend:
    name = ""

    def __init__(self, path: str):
        self.path = str(path)

    def run_ac(self, deck: str, nodes: list[str], timeout: float = 60.0) -> dict:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{self.name}:{self.path}"


class NgspiceBackend(Backend):
    name = "ngspice"

    def run_ac(self, deck: str, nodes: list[str], timeout: float = 60.0) -> dict:
        with tempfile.TemporaryDirectory() as d:
            cir = Path(d) / "deck.cir"
            data = Path(d) / "out.txt"
            ctrl = (".control\nrun\nset wr_singlescale\nwrdata " + str(data).replace("\\", "/")
                    + " " + " ".join(f"vdb({n})" for n in nodes) + "\n.endc\n")
            cir.write_text(deck.replace(".end\n", ctrl + ".end\n"), encoding="utf-8")
            r = subprocess.run([self.path, "-b", str(cir)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout, check=False)
            if not data.is_file():
                raise RuntimeError(f"ngspice produced no data: {(r.stderr or r.stdout)[-400:]}")
            out = {n: [] for n in nodes}
            for line in data.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                if len(parts) < 1 + len(nodes):
                    continue
                try:
                    f = float(parts[0])
                    vals = [float(x) for x in parts[1:1 + len(nodes)]]
                except ValueError:
                    continue
                for n, v in zip(nodes, vals):
                    out[n].append((f, v))
            return out


class LtspiceBackend(Backend):
    name = "ltspice"

    def run(self, deck: str, folder: Path, timeout: float = 120.0) -> tuple[Path, str]:
        """Run `deck` in `folder`; returns (.raw path, log text)."""
        cir = folder / "deck.cir"
        cir.write_text(deck, encoding="utf-8")
        run_tree([self.path, "-b", str(cir)], folder, timeout)
        log = folder / "deck.log"
        text = log.read_bytes().decode("utf-16-le" if log.is_file() and
                                       log.read_bytes()[1:2] == b"\x00" else "utf-8",
                                       errors="replace") if log.is_file() else ""
        return folder / "deck.raw", text

    def run_ac(self, deck: str, nodes: list[str], timeout: float = 60.0) -> dict:
        with tempfile.TemporaryDirectory() as d:
            raw, log = self.run(deck, Path(d), timeout)
            if not raw.is_file():
                raise RuntimeError(f"LTspice produced no data: {log[-400:]}")
            res = read_raw(raw)
            data = {k.lower(): v for k, v in res["data"].items()}
            freq = [z.real for z in data.get("frequency", [])]
            out = {}
            for n in nodes:
                vals = data.get(f"v({n.lower()})")
                if vals is None:
                    raise RuntimeError(f"LTspice: node {n} not in the .raw file")
                out[n] = [(f, _db(z)) for f, z in zip(freq, vals)]
            return out


def _ltspice_candidates() -> list[Path]:
    out = []
    env = os.environ.get("LTSPICE_PATH")
    if env:
        out.append(Path(env))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and os.path.isabs(entry):
            out += [Path(entry) / n for n in LTSPICE_NAMES]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "Programs" / "ADI" / "LTspice" / "LTspice.exe")
    out.append(Path(r"C:\Program Files\ADI\LTspice\LTspice.exe"))
    out.append(Path(r"C:\Program Files\LTC\LTspiceXVII\XVIIx64.exe"))
    return out


def find_ltspice() -> str | None:
    for p in _ltspice_candidates():
        if p.is_absolute() and p.is_file():
            return str(p)
    return None


def find_ngspice() -> str | None:
    return shutil.which("ngspice")


def detect(simulator: str = "auto") -> Backend | None:
    """The backend to use: `ngspice`, `ltspice`, or `auto` (ngspice, else
    LTspice); None when not installed."""
    if simulator not in SIMULATORS:
        raise ValueError(f"simulator {simulator!r} not in {list(SIMULATORS)}")
    if simulator in ("auto", "ngspice"):
        ng = find_ngspice()
        if ng:
            return NgspiceBackend(ng)
        if simulator == "ngspice":
            return None
    lt = find_ltspice()
    return LtspiceBackend(lt) if lt else None


def as_backend(sim) -> Backend:
    """A Backend from a Backend or an ngspice path string."""
    return sim if isinstance(sim, Backend) else NgspiceBackend(sim)
