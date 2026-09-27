"""
core/cell_cache.py — Per-cell Cache Schema (I3, V3 prerequisite M4.4)
=====================================================================
Forward-looking cache schema keyed at the (mutant, test-input) CELL granularity,
built on the M4.3 identity layer. It is the substrate a future V3 incremental
run (N10) / partial reuse (I3) will use.

SAFETY — REUSE IS HARD-DISABLED (double-gated):
  1. Every CellCache instance defaults to `enabled=False`. While disabled:
       - `lookup()` ALWAYS returns None (no hit can ever be served), and
       - `store()` is a no-op.
  2. Nothing in the pipeline imports or calls this module (it is unreachable),
     so even the disabled paths are never exercised in M4.
A wrong cache hit causes silent wrong mutation scores; therefore M4.4 only
*defines the schema* and keeps reuse off. Activation, on-disk read/write, and a
mandatory drift test are deferred to V3 (N10), not done here.

Schema is versioned so entries from a different scheme can never match.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.identity import IDENTITY_SCHEME_VERSION, mutant_test_identity

logger = logging.getLogger(__name__)

# Bump if the cell-cache record schema changes (independent of identity scheme).
CELL_CACHE_SCHEMA_VERSION = 1


def composite_cell_key(mutant: Any, test_input_json: str) -> str:
    """Stable composite key for one (mutant, test-input) execution cell.

    Combines the cell-cache schema version, the identity scheme version, and the
    per-cell identity (which itself hashes the mutant's semantic fields + code).
    Position-independent and edit-sensitive — safe as a reuse key *when reuse is
    eventually enabled with a drift test*."""
    cell = mutant_test_identity(mutant, test_input_json)
    return f"cc{CELL_CACHE_SCHEMA_VERSION}.id{IDENTITY_SCHEME_VERSION}.{cell}"


class CellCache:
    """Dormant per-cell cache. Reuse is OFF unless explicitly enabled AND wired
    (neither happens in M4). The result tuple stored/served is exactly the
    sandbox result shape `(status, payload)`."""

    def __init__(self, cache_dir: str = "output/cell_cache", *, enabled: bool = False) -> None:
        self.cache_dir = cache_dir
        self._enabled = bool(enabled)
        # NOTE: directory is intentionally NOT created here; while disabled the
        # cache touches no filesystem and has no side effects.

    @property
    def enabled(self) -> bool:
        return self._enabled

    def lookup(self, key: str) -> Optional[tuple]:
        """Return a cached cell result, or None.

        While disabled (the M4 default) this ALWAYS returns None — no reuse can
        be served under any circumstance."""
        if not self._enabled:
            return None
        # Reuse path is intentionally unimplemented in M4 (deferred to V3 N10,
        # to be added together with a drift test). Fail safe → no hit.
        logger.debug("[CellCache] enabled but read path not implemented in M4 "
                     "→ returning None (no reuse).")
        return None

    def store(self, key: str, result: tuple) -> bool:
        """Persist a cell result. No-op while disabled (the M4 default)."""
        if not self._enabled:
            return False
        # Write path intentionally deferred to V3; no-op to remain side-effect free.
        return False


def make_cell_cache(config: dict) -> CellCache:
    """Construct a CellCache from config. Defaults to DISABLED.

    Even if `config.sandbox.cell_cache_enabled` were set true, no pipeline code
    calls lookup/store in M4, so reuse remains unreachable. The flag exists only
    so V3 can wire activation behind it (with the required drift test)."""
    enabled = bool((config.get("sandbox", {}) or {}).get("cell_cache_enabled", False))
    return CellCache(enabled=enabled)
