"""
strategies/registry.py — Strategy Registry (F4, V3 prerequisite M4.2)
=====================================================================
An ordered registry of TestStrategy plug-ins. The pipeline obtains its
strategies via `get_enabled(config)` instead of importing strategy modules
directly. Built-in adapters wrap the existing strategy functions verbatim:

    acoc   → strategies.acoc_strategy.generate(config)            (default seed)
    llm    → strategies.llm_strategy.generate(mutants, llm_client, config)
    domain → strategies.domain_strategy.generate(config)

Registration order is **acoc, llm, domain** — identical to the legacy
`pool_a + pool_b + pool_c` (A+B+C) merge order in test_pool_builder. With all
three enabled in this order, the merged/deduplicated pool is byte-identical to
V2. A future N9 adaptive selector can change what `get_enabled` returns (subset
/ order / new plug-ins) WITHOUT touching the pipeline.

ADDITIVE: no strategy logic is changed here; these are thin adapters.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List

from strategies import acoc_strategy, domain_strategy, llm_strategy
from strategies.base import StrategyContext, TestStrategy

logger = logging.getLogger(__name__)


class _FnStrategy:
    """Adapter that turns an existing strategy function into a TestStrategy.

    `fn(context) -> list[dict]` is a closure that calls the underlying module
    function with exactly its current arguments — preserving behavior."""

    def __init__(self, name: str, fn: Callable[[StrategyContext], List[dict]]) -> None:
        self.name = name
        self._fn = fn

    def generate(self, context: StrategyContext) -> List[dict]:
        return self._fn(context)


# ── Built-in adapters (1:1 with the legacy calls; logic unchanged) ────────────
_ACOC = _FnStrategy("acoc",   lambda ctx: acoc_strategy.generate(ctx.config))
_LLM = _FnStrategy("llm",     lambda ctx: llm_strategy.generate(ctx.mutants,
                                                                ctx.llm_client,
                                                                ctx.config))
_DOMAIN = _FnStrategy("domain", lambda ctx: domain_strategy.generate(ctx.config))

# Ordered registry. ORDER MATTERS: must equal the legacy A+B+C merge order so
# the concatenated/deduplicated pool stays byte-identical to V2.
_DEFAULT_ORDER: List[str] = ["acoc", "llm", "domain"]
_REGISTRY: Dict[str, TestStrategy] = {
    "acoc":   _ACOC,
    "llm":    _LLM,
    "domain": _DOMAIN,
}


def register(strategy: TestStrategy, *, position: int | None = None) -> None:
    """Register/replace a strategy plug-in. `position` inserts into the default
    order (append if None). Provided for future plug-ins (e.g. N11 fuzzing);
    unused by the V2-parity default path."""
    _REGISTRY[strategy.name] = strategy
    if strategy.name in _DEFAULT_ORDER:
        return
    if position is None:
        _DEFAULT_ORDER.append(strategy.name)
    else:
        _DEFAULT_ORDER.insert(position, strategy.name)


def get_enabled(config: dict) -> List[TestStrategy]:
    """Return the enabled strategies in deterministic order.

    Default = the three built-ins in legacy order (acoc, llm, domain), which
    reproduces the V2 pool exactly. Optional opt-in override via
    `config.test_pool.strategies` (an ordered list of names) is honored if
    present; absent → full default order. A future N9 adaptive selector can
    drive this without changing the pipeline.
    """
    names = (config.get("test_pool", {}) or {}).get("strategies")
    if not names:
        names = _DEFAULT_ORDER
    enabled: List[TestStrategy] = []
    for n in names:
        s = _REGISTRY.get(n)
        if s is None:
            logger.warning("  [Registry] unknown strategy '%s' — skipped", n)
            continue
        enabled.append(s)
    return enabled
