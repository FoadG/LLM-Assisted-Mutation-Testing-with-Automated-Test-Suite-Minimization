"""
infra/reproducibility.py — Reproducibility Envelope (N20 / T1.4)
=================================================================
Captures the environment that produced a run so results are reproducible:
Python version, key dependency versions, OS, and the ACOC RNG seed
(currently hardcoded as 42 in strategies/acoc_strategy.py).

Attached to the Manifest envelope. Becomes fully meaningful after I1 makes
execution timing-stable; for now it pins everything that is already
deterministic in --no-llm mode.
"""

from __future__ import annotations

import platform
import sys

DEFAULT_ACOC_SEED = 42


def _dist_version(name: str) -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version(name)
        except PackageNotFoundError:
            return None
    except Exception:
        return None


def capture_env(seed: int = DEFAULT_ACOC_SEED) -> dict:
    """Return a JSON-serializable snapshot of the run environment."""
    deps = {name: _dist_version(name) for name in ("pulp", "coverage", "pytest")}
    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "implementation": platform.python_implementation(),
        "dependencies": deps,
        "acoc_seed": seed,
    }