"""
core/feedback_analysis.py — Post-Processing Reasoning Layer (I6, V2 M2)
=======================================================================
A deterministic, read-only reasoning pass over already-computed pipeline
results. It does NOT execute code, call any LLM, or mutate any pipeline data;
it only inspects the final mutant statuses + ILP result and emits structured,
heuristic guidance about *surviving* mutants (those that no test killed and
that I4 did not flag as provably equivalent).

CONTRACT:
  - Pure function of (mutants, ilp_result, equivalent_ids). No I/O, no side
    effects, no network. Output is consumed by the reporter (I7) and placed in
    an additive `analysis.feedback` section.
  - Survivors = mutants with status == SUSPECTED, minus the I4 equivalent set.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import List

from core.contracts import STATUS_SUSPECTED

logger = logging.getLogger(__name__)

# Heuristic, category-driven reasoning. No execution — purely structural advice.
_CATEGORY_REASON = {
    "REL": (
        "Relational operator change not observed by any selected test.",
        "Add an input at the comparison boundary (equal / off-by-one) for "
        "function '{fn}' so the changed relation flips the outcome.",
    ),
    "ARITH": (
        "Arithmetic change did not affect any observed output.",
        "Add an input where the operand magnitude/sign at line {line} changes "
        "the returned value of '{fn}'.",
    ),
    "LOGICAL": (
        "Boolean/logical change never toggled a branch under test.",
        "Add an input that flips the predicate at line {line} so '{fn}' takes "
        "the other branch.",
    ),
    "INTEGRATION": (
        "Integration-level change (call/arg/return) was not differentiated.",
        "Add an input that distinguishes the integrated call at line {line} in "
        "'{fn}' (vary argument/order/return usage).",
    ),
}
_DEFAULT_REASON = (
    "Mutant survived all selected tests; behavioural difference unobserved.",
    "Add a targeted test for '{fn}' around line {line} exercising "
    "operator '{op}'.",
)


def _insight(m) -> dict:
    reason, suggestion = _CATEGORY_REASON.get(m.category, _DEFAULT_REASON)
    fmt = {"fn": m.function_name, "line": m.line, "op": m.operator_name}
    return {
        "mutant_id":   m.id,
        "function":    m.function_name,
        "line":        m.line,
        "category":    m.category,
        "operator":    m.operator_name,
        "reason":      reason.format(**fmt),
        "suggestion":  suggestion.format(**fmt),
    }


def analyze(mutants: List, ilp_result, equivalent_ids: List[str] | None = None) -> dict:
    """Produce structured reasoning over surviving (non-equivalent) mutants.

    Returns an additive `feedback` annotation. Empty `insights` when every
    mutant is killed or proven equivalent.
    """
    equivalent = set(equivalent_ids or [])
    survivors = [
        m for m in mutants
        if getattr(m, "status", None) == STATUS_SUSPECTED and m.id not in equivalent
    ]

    insights = [_insight(m) for m in survivors]
    by_category = dict(Counter(m.category for m in survivors))

    return {
        "survivors":          len(survivors),
        "equivalent_excluded": len(equivalent),
        "by_category":        by_category,
        "insights":           insights,
        "note": (
            "Deterministic heuristic guidance over existing results. "
            "No code execution, no LLM, no pipeline mutation."
        ),
    }
