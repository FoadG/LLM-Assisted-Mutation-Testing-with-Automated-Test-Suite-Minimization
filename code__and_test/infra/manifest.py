"""
infra/manifest.py — Run Manifest (I2 / T1.3)
=============================================
Records phase timings and structured events for a run and writes a
machine-readable run_manifest.json. Atomic write (tmp + os.replace),
reusing the durability pattern from utils/cache_manager.py.

Schema:
  {schema_version, run_id, envelope:{}, phases:[{name,duration_s,counts}],
   events:[{ts, name, fields}]}
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA_VERSION = 1


class Manifest:
    """Collects phase timings + events; finalize() writes atomically."""

    def __init__(self, envelope: dict[str, Any] | None = None) -> None:
        self.run_id: str = uuid.uuid4().hex[:12]
        self.envelope: dict[str, Any] = dict(envelope or {})
        self.phases: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._t0 = time.monotonic()

    @contextmanager
    def phase(self, name: str, **counts: Any) -> Iterator["Manifest"]:
        start = time.monotonic()
        try:
            yield self
        finally:
            self.phases.append({
                "name": name,
                "duration_s": round(time.monotonic() - start, 4),
                "counts": dict(counts),
            })

    def event(self, name: str, **fields: Any) -> None:
        self.events.append({
            "ts": round(time.monotonic() - self._t0, 4),
            "name": name,
            "fields": fields,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "envelope": self.envelope,
            "phases": self.phases,
            "events": self.events,
            "total_duration_s": round(time.monotonic() - self._t0, 4),
        }

    def finalize(self, path: str) -> None:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # atomic


# ── I2: versioned-schema validation (additive) ───────────────────────────────
_REQUIRED_TOP_KEYS = {
    "schema_version", "run_id", "envelope", "phases", "events",
    "total_duration_s",
}
_REQUIRED_PHASE_KEYS = {"name", "duration_s", "counts"}
_REQUIRED_EVENT_KEYS = {"ts", "name", "fields"}


def validate(manifest: dict[str, Any]) -> list[str]:
    """Validate a manifest dict against the current schema version.

    Returns a list of human-readable error strings (empty list == valid).
    Pure function; performs no I/O. Used by the orchestrator after finalize().
    """
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest is not a dict"]

    missing = _REQUIRED_TOP_KEYS - set(manifest.keys())
    if missing:
        errors.append(f"missing top-level keys: {sorted(missing)}")

    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version mismatch: got {manifest.get('schema_version')!r}, "
            f"expected {SCHEMA_VERSION}"
        )
    if not isinstance(manifest.get("run_id"), str) or not manifest.get("run_id"):
        errors.append("run_id must be a non-empty string")
    if not isinstance(manifest.get("envelope"), dict):
        errors.append("envelope must be a dict")
    if not isinstance(manifest.get("total_duration_s"), (int, float)):
        errors.append("total_duration_s must be numeric")

    phases = manifest.get("phases")
    if not isinstance(phases, list):
        errors.append("phases must be a list")
    else:
        for i, p in enumerate(phases):
            if not isinstance(p, dict) or not _REQUIRED_PHASE_KEYS <= set(p.keys()):
                errors.append(f"phases[{i}] missing keys {sorted(_REQUIRED_PHASE_KEYS)}")
            elif not isinstance(p.get("duration_s"), (int, float)):
                errors.append(f"phases[{i}].duration_s must be numeric")

    events = manifest.get("events")
    if not isinstance(events, list):
        errors.append("events must be a list")
    else:
        for i, e in enumerate(events):
            if not isinstance(e, dict) or not _REQUIRED_EVENT_KEYS <= set(e.keys()):
                errors.append(f"events[{i}] missing keys {sorted(_REQUIRED_EVENT_KEYS)}")

    return errors


def is_valid(manifest: dict[str, Any]) -> bool:
    """Convenience boolean wrapper around validate()."""
    return not validate(manifest)