"""
core/history_readback.py — History Read-back Support (N14, V3 prerequisite M4.5)
================================================================================
Integrates the existing history READ APIs (`utils.history_tracker.load_history`
and `get_trend`) into a single, safe read-back surface that future V3 features
(N9 adaptive strategy, N18 metrics/trends, N10 incremental decisions) can call.

SAFETY / SCOPE:
  - READ-ONLY. This module never writes history. The existing write API
    (`history_tracker.record_run`) is untouched, and — per repository evidence —
    is not currently invoked on the run path; M4.5 does NOT add a write.
  - PASSIVE. Nothing in the pipeline imports this module, so it changes no
    mutation result, no manifest, no report. It only makes priors *available*.
  - SAFE-EMPTY. With no history file it returns a well-formed "no_data" result;
    it never raises on a missing/corrupt file (delegates to the tolerant
    load_history, which returns [] on error).

Future consumers (V3) call `read_back(out_dir)` to obtain priors + trend; the
decision logic that USES them (e.g. N9) is out of M4 scope.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from utils.history_tracker import get_trend, load_history

logger = logging.getLogger(__name__)


def available(out_dir: str) -> bool:
    """True iff at least one prior run is recorded. Safe (no exceptions)."""
    return len(load_history(out_dir)) > 0


def read_back(out_dir: str) -> dict[str, Any]:
    """Return priors + computed trend for `out_dir`.

    Shape: {"history": list[dict], "trend": dict}. Read-only; safe-empty when no
    history exists (trend -> {"runs": 0, "trend": "no_data", "scores": []})."""
    history = load_history(out_dir)
    return {"history": history, "trend": get_trend(history)}


def latest(out_dir: str) -> Optional[dict]:
    """The most recent run record, or None if history is empty. Read-only."""
    history = load_history(out_dir)
    return history[-1] if history else None
