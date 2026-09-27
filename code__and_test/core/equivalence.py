"""
core/equivalence.py — Equivalent-Mutant Detection (I4, V2 M2)
=============================================================
A *conservative, sound* detector for provably-equivalent mutants.

DESIGN CONTRACT (safety-critical):
  - This module is ADVISORY. It NEVER mutates MutantRecord.status and NEVER
    feeds back into the V1 scoring path (ilp_solver / reporter summary). It only
    produces an annotation that the reporter (I7) renders in an additive
    `analysis` section. Therefore a misclassification here can never change the
    frozen V1 result (raw_score / adjusted_score / killed / suspected / alive).
  - It only ever *flags* a mutant; it errs toward NOT flagging (zero
    false-positive intent). Only mutants that survived every test
    (status == SUSPECTED) are considered, because equivalence only matters for
    survivors.

METHOD (sound by construction):
  Both the original source and the mutant source are normalised by an
  identity-folding AST pass that rewrites ONLY value-preserving identities
  (x*1, 1*x, x+0, 0+x, x-0, x/1, x//1, x**1, x|0, x^0, x<<0, x>>0; and the
  no-op-operand removal for `and True` / `or False`). If a mutant's normalised
  AST is structurally identical to the normalised original, the operator change
  was provably a no-op and the mutant is equivalent. Plain textual
  canonicalisation in mutant_generator does NOT catch these (e.g. `a+0` -> `a-0`
  both fold to `a`), so this adds real signal while staying sound.

  NOTE: the numeric identities assume numeric operands. Because the result is
  advisory only (never alters V1 scoring), this assumption cannot corrupt the
  pipeline; the worst case is one advisory false-positive in the analysis block.
"""

from __future__ import annotations

import ast
import logging
from typing import List

from core.contracts import STATUS_SUSPECTED

logger = logging.getLogger(__name__)


def _is_int_const(node: ast.AST, value: int) -> bool:
    """True iff node is an int Constant equal to `value` (bool excluded)."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
        and node.value == value
    )


def _is_true_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_false_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


class _IdentityFolder(ast.NodeTransformer):
    """Rewrites only value-preserving identities. Children folded first."""

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        op, left, right = node.op, node.left, node.right

        if isinstance(op, ast.Mult):
            if _is_int_const(right, 1):
                return left
            if _is_int_const(left, 1):
                return right
        elif isinstance(op, ast.Add):
            if _is_int_const(right, 0):
                return left
            if _is_int_const(left, 0):
                return right
        elif isinstance(op, ast.Sub):
            if _is_int_const(right, 0):
                return left
        elif isinstance(op, (ast.Div, ast.FloorDiv)):
            if _is_int_const(right, 1):
                return left
        elif isinstance(op, ast.Pow):
            if _is_int_const(right, 1):
                return left
        elif isinstance(op, (ast.BitOr, ast.BitXor, ast.LShift, ast.RShift)):
            if _is_int_const(right, 0):
                return left
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Or):
            kept = [v for v in node.values if not _is_false_const(v)]
        elif isinstance(node.op, ast.And):
            kept = [v for v in node.values if not _is_true_const(v)]
        else:
            kept = node.values
        if not kept:
            return node
        if len(kept) == 1:
            return kept[0]
        node.values = kept
        return node


def _normalized_dump(source: str) -> str | None:
    """Return the identity-folded AST dump of `source`, or None on parse error."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    folded = _IdentityFolder().visit(tree)
    ast.fix_missing_locations(folded)
    # include_attributes=False -> compare structure/values, ignore positions
    return ast.dump(folded, include_attributes=False)


def detect_equivalent(mutants: List, original_source: str) -> List[str]:
    """Return the ids of mutants that are PROVABLY equivalent to the original.

    Only SUSPECTED (survived-all-tests) mutants are considered. Pure function;
    performs no I/O and never mutates the input records.
    """
    norm_orig = _normalized_dump(original_source)
    if norm_orig is None:
        return []

    equivalent: List[str] = []
    for m in mutants:
        if getattr(m, "status", None) != STATUS_SUSPECTED:
            continue
        norm_mut = _normalized_dump(getattr(m, "code", "") or "")
        if norm_mut is not None and norm_mut == norm_orig:
            equivalent.append(m.id)
    return equivalent


def summarize(mutants: List, ilp_result, equivalent_ids: List[str]) -> dict:
    """Build the additive equivalence annotation for the reporter (I7).

    `equivalence_adjusted_score` is ADVISORY: killed / (total - equivalents).
    It does NOT replace ilp_result.adjusted_score (the frozen V1 metric).
    """
    count = len(equivalent_ids)
    total = ilp_result.n_total
    killed = ilp_result.n_killed
    denom = max(total - count, 1)
    eq_adj = round(killed / denom * 100.0, 2) if total else 0.0
    return {
        "method": "ast_identity_folding",
        "count": count,
        "equivalent_ids": list(equivalent_ids),
        "equivalence_adjusted_score": eq_adj,
        "note": (
            "Advisory only. Provably-equivalent survivors detected by sound "
            "identity folding. Does NOT alter V1 raw_score/adjusted_score."
        ),
    }
