"""Logging configured to stderr.

CRITICAL: MCP servers using STDIO transport communicate over stdout. Any
print() or log handler attached to stdout corrupts the JSON-RPC stream and
breaks the connection. Always log to stderr.
"""

from __future__ import annotations

import logging
import os
import re
import sys

_CONFIGURED = False

# Distributor APIs (Mouser, element14/Farnell) take the key as a query
# parameter, and httpx logs every request URL at INFO. Keep those URLs out of
# the log, and redact key-like query values from any record that still has one.
_SECRET_RE = re.compile(r"(?i)((?:api[_.-]?key|apikey|token|secret|client_secret|"
                        r"access_token|callInfo\.apiKey)=)[^&\s\"']+")


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never drop a record over formatting
            return True
        red = _SECRET_RE.sub(lambda m: m.group(1) + "***", msg)
        if red != msg:
            record.msg, record.args = red, None
        return True


def setup_logging(level: int | str | None = None) -> logging.Logger:
    """Configure root logging to stderr. Idempotent.

    Level resolution: explicit arg > KICAD_CLAUDE_LOG_LEVEL env > INFO.
    """
    global _CONFIGURED

    if level is None:
        level = os.environ.get("KICAD_CLAUDE_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    if not _CONFIGURED:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.addFilter(RedactSecrets())
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(level)
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _CONFIGURED = True
    else:
        logging.getLogger().setLevel(level)

    return logging.getLogger("kicad-claude")
