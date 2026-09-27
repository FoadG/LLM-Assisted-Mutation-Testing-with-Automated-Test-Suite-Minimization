"""
infra/logging_setup.py — Structured Logging (I2 / T1.1)
========================================================
Central logging configuration. `configure()` is idempotent so it is safe to
call from both cli/main.py and main.main() (direct `python main.py`).

Default: a human-readable console formatter that prints the bare message,
preserving the pre-existing console look (the prints it replaces emitted the
message only). `--json-logs` switches to a structured JSON line formatter.
"""

from __future__ import annotations

import json
import logging
import sys

_CONFIGURED = False


class _HumanFormatter(logging.Formatter):
    """Emit the bare message for INFO (print-compatible); annotate WARNING+."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno <= logging.INFO:
            return msg
        return f"[{record.levelname}] {msg}"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure(level: int | str = logging.INFO, json_mode: bool = False) -> None:
    """
    Configure the root logger. Idempotent: a second call updates the level
    but does not stack duplicate handlers.
    """
    global _CONFIGURED
    root = logging.getLogger()

    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    if _CONFIGURED:
        root.setLevel(level)
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter() if json_mode else _HumanFormatter())
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


def reset() -> None:
    """Test helper: forget configuration so a test can reconfigure cleanly."""
    global _CONFIGURED
    logging.getLogger().handlers.clear()
    _CONFIGURED = False