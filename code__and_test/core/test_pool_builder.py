"""
core/test_pool_builder.py — Phase 2
=====================================
Coordinates the three test-generation strategies and produces a
deduplicated, merged test pool.

Execution order:
  A) ACOC   — Cartesian product of parameter partitions
  B) LLM    — targeted tests based on mutant operator/line diff
  C) Domain — sort and search algorithm edge cases
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List

from strategies.base import StrategyContext
from strategies.registry import get_enabled
from utils.json_utils import deduplicate
import logging

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Import only for type checking to avoid circular imports at runtime
    from core.mutant_generator import MutantRecord


def build(
    mutants:    List["MutantRecord"],
    llm_client: Any,
    config:     dict,
) -> List[dict]:
    """
    Build the deduplicated test pool from the registered strategies.

    Args:
        mutants:    Phase-1 output (list[MutantRecord])
        llm_client: LLMClient or _DummyLLM — must have .call(prompt, temperature)
        config:     full config.json dict

    Returns:
        list[dict] where each record is {"function": str, "inputs": {...}}

    M4.2 (F4): strategies are obtained from the registry instead of hard-coded
    imports. The default registry yields acoc, llm, domain in that order, so the
    merged/deduplicated pool is byte-identical to V2. A future N9 adaptive
    selector can change the enabled set/order without editing this function.
    """
    logger.info("\n[PHASE-2] Building test pool...")

    context = StrategyContext(mutants=mutants, llm_client=llm_client, config=config)
    _label = {"acoc": "A-ACOC", "llm": "B-LLM", "domain": "C-Domain"}

    merged: List[dict] = []
    for strategy in get_enabled(config):
        produced = strategy.generate(context)
        merged += produced
        logger.info(f"  [{_label.get(strategy.name, strategy.name)}]   "
                    f"{len(produced)} tests")

    unique = deduplicate(merged)

    removed = len(merged) - len(unique)
    logger.info(
        f"  [POOL] Total: {len(merged)} | "
        f"Duplicates removed: {removed} | "
        f"Final: {len(unique)}"
    )
    logger.info(f"[PHASE-2] ✓ Final pool: {len(unique)} tests\n")

    return unique