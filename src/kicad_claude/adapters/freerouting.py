"""Freerouting JAR runner.

Invokes `java -jar freerouting.jar -de input.dsn -do output.ses
--router.autorouter.max_passes=<passes>` (Freerouting 2.5.0, Java 25+)
with timeouts and parses progress lines from stdout.

Required environment:
    FREEROUTING_JAR     (path to freerouting-2.x.x.jar; default: ./third_party/freerouting.jar)
Optional:
    JAVA_BIN            (default: `java` on PATH)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("kicad-claude.adapters.freerouting")


class FreeroutingError(RuntimeError):
    pass


# Match the summary lines Freerouting 2.5 prints, e.g.:
#   "Auto-routing pass #5 on board '…' was completed in 0.12 seconds with
#    score 619.05 (6 unrouted and 0 violations)"
#   "Auto-routing stage completed: started with 10 unrouted nets, completed in
#    1.19 seconds, final score: 619.05 (6 unrouted and 0 violations), …"
_PASS = re.compile(r"auto-routing pass #(\d+)", re.IGNORECASE)
_RESULT = re.compile(
    r"score:?\s+(?:of\s+)?(\d+(?:\.\d+)?)\s+\((\d+)\s+unrouted\s+and\s+(\d+)\s+violations\)",
    re.IGNORECASE,
)
_STAGE = re.compile(
    r"auto-routing stage completed: started with (\d+) unrouted nets?, "
    r"completed in (\d+(?:\.\d+)?) seconds",
    re.IGNORECASE,
)


def find_freerouting_jar() -> Path | None:
    """Resolve the Freerouting JAR path.

    Order: FREEROUTING_JAR env > `<repo>/third_party/freerouting*.jar`. The
    release downloads are named `freerouting-2.5.0.jar`, so the versioned names
    are matched too, newest version first.
    """
    env = os.environ.get("FREEROUTING_JAR")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p

    third_party = Path(__file__).resolve().parents[3] / "third_party"
    candidate = third_party / "freerouting.jar"
    if candidate.is_file():
        return candidate
    versioned = sorted(
        (p for p in third_party.glob("freerouting*.jar") if p.is_file()),
        key=lambda p: _version_key(p.stem),
        reverse=True,
    )
    return versioned[0] if versioned else None


def _version_key(stem: str) -> tuple[int, ...]:
    """Sort key from a jar's name: `freerouting-2.5.0` -> (2, 5, 0)."""
    digits = re.findall(r"\d+", stem)
    return tuple(int(d) for d in digits) or (0,)


def find_java() -> Path | None:
    custom = os.environ.get("JAVA_BIN")
    if custom and Path(custom).is_file():
        return Path(custom)
    java = shutil.which("java")
    return Path(java) if java else None


def _ensure_environment() -> tuple[Path, Path]:
    jar = find_freerouting_jar()
    if jar is None:
        raise FreeroutingError(
            "freerouting.jar not found. Set FREEROUTING_JAR or download it to "
            "third_party/freerouting.jar (https://github.com/freerouting/freerouting/releases)"
        )
    java = find_java()
    if java is None:
        raise FreeroutingError("`java` not on PATH; install Java 25+ (Freerouting 2.5.0)")
    return jar, java


def _parse_stats(combined_output: str) -> dict:
    """Pull the routing summary Freerouting 2.5 prints into a dict. Best-effort.

    `passes_done`, `score`, `unrouted` and `violations` come from the last
    pass / optimizer line (the final state); `unrouted_start` and
    `duration_s` from the auto-routing stage summary.
    """
    stats: dict = {}
    passes = _PASS.findall(combined_output)
    if passes:
        stats["passes_done"] = int(passes[-1])
    results = _RESULT.findall(combined_output)
    if results:
        score, unrouted, violations = results[-1]
        stats["score"] = float(score)
        stats["unrouted"] = int(unrouted)
        stats["violations"] = int(violations)
    stage = _STAGE.search(combined_output)
    if stage:
        stats["unrouted_start"] = int(stage.group(1))
        stats["duration_s"] = float(stage.group(2))
    return stats


def route(
    dsn_path: Path,
    ses_path: Path,
    *,
    passes: int = 100,
    threads: int | None = None,
    timeout_seconds: float = 300.0,
    extra_args: list[str] | None = None,
) -> dict:
    """Run Freerouting on `dsn_path` and write the result to `ses_path`.

    Returns a dict with `returncode`, `stats`, and `stdout_tail`/`stderr_tail`.
    Times out hard at `timeout_seconds` (Freerouting can run forever on
    pathological boards).
    """
    jar, java = _ensure_environment()
    dsn_path = Path(dsn_path).expanduser().resolve()
    ses_path = Path(ses_path).expanduser().resolve()
    if not dsn_path.is_file():
        raise FileNotFoundError(dsn_path)

    cmd = [
        str(java),
        "-jar",
        str(jar),
        "-de",
        str(dsn_path),
        "-do",
        str(ses_path),
        f"--router.autorouter.max_passes={passes}",
    ]
    if threads is not None:
        cmd += ["-mt", str(threads)]
    if extra_args:
        cmd += list(extra_args)

    logger.info("running freerouting: %s", " ".join(cmd))
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise FreeroutingError(
            f"Freerouting timed out after {timeout_seconds}s "
            f"(consider larger timeout, fewer passes, or simpler design)"
        ) from e

    combined = (r.stdout or "") + "\n" + (r.stderr or "")

    if not ses_path.is_file():
        # Even if rc == 0, no SES means routing didn't complete.
        raise FreeroutingError(
            f"Freerouting did not produce {ses_path}. "
            f"rc={r.returncode}, stderr tail: {r.stderr[-400:]}"
        )

    return {
        "returncode": r.returncode,
        "ses_path": str(ses_path),
        "stats": _parse_stats(combined),
        "stdout_tail": (r.stdout or "")[-1000:],
        "stderr_tail": (r.stderr or "")[-400:],
    }
