"""
core/run_context.py — Immutable Run Envelope (T0.5)
====================================================
Scaffolding only. In later milestones (M2/M3) this immutable object will carry
the resolved config, RNG seeds, output paths, and injected services
(Executor, LLMClient, Cache, History, Manifest) so components stop receiving
long positional argument lists.

For now it is constructed in main() and intentionally NOT threaded downstream;
it changes no behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class RunContext:
    """Immutable per-run envelope (scaffolding for V2)."""
    config:      dict
    out_dir:     str
    seed:        int = 42          # current ACOC seed
    llm_enabled: bool = False
    verbose:     bool = False
    # Injected services are added in later milestones; kept optional+None now.
    services:    dict[str, Any] = field(default_factory=dict)

    def get_service(self, name: str) -> Optional[Any]:
        return self.services.get(name)