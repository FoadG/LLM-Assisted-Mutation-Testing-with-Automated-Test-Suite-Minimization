"""
strategies/base.py — Test-Strategy Protocol (F4, V3 prerequisite M4.2)
======================================================================
Defines the plug-in contract that test-generation strategies implement so the
pipeline iterates a registry instead of hard-coding strategy calls. ADDITIVE:
the existing strategy modules (acoc/llm/domain) keep their logic unchanged; they
are wrapped as registered plug-ins.

This is the seam that lets a future N9 adaptive selector reorder/enable
strategies WITHOUT editing the pipeline — but in M4.2 the default registry
yields the same three strategies in the same order, so behavior is identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Protocol, runtime_checkable


@dataclass(frozen=True)
class StrategyContext:
    """Read-only inputs a strategy may use. Carries exactly what the current
    strategy functions consume, so adapters are 1:1 with today's calls."""
    mutants: List[Any]
    llm_client: Any
    config: dict


@runtime_checkable
class TestStrategy(Protocol):
    """A test-generation strategy plug-in.

    `name`     stable identifier (used for logging / future selection).
    `generate` returns a list of test records [{"function": str, "inputs": {...}}].
    """
    name: str

    def generate(self, context: StrategyContext) -> List[dict]: ...
