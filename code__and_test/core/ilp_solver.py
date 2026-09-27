"""
core/ilp_solver.py — Phase 4
==============================
Selects the minimum test suite using Integer Linear Programming.
Falls back to Greedy Set Cover if PuLP is unavailable or times out.

Dependency: pip install pulp
"""

from __future__ import annotations

import sys
from typing import List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

try:
    import pulp
except ImportError:
    pulp = None  # type: ignore[assignment]


# ══════════════════════════════════════════════════════════════════════════════
# Result container
# ══════════════════════════════════════════════════════════════════════════════

class ILPResult:
    """Complete result from Phase 4 (or Phase 5 re-solve)."""

    def __init__(
        self,
        selected_tests:    List[int],
        suspected_indices: List[int],
        raw_score:         float,
        adjusted_score:    float,
        n_killed:          int,
        n_total:           int,
        n_suspected:       int,
        feasible:          bool,
    ) -> None:
        self.selected_tests    = selected_tests
        self.suspected_indices = suspected_indices
        self.raw_score         = raw_score
        self.adjusted_score    = adjusted_score
        self.n_killed          = n_killed
        self.n_total           = n_total
        self.n_suspected       = n_suspected
        self.feasible          = feasible

    def __repr__(self) -> str:
        return (
            f"ILPResult("
            f"tests={len(self.selected_tests)}, "
            f"killed={self.n_killed}/{self.n_total}, "
            f"raw={self.raw_score:.1f}%, "
            f"adj={self.adjusted_score:.1f}%, "
            f"suspected={self.n_suspected})"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Primary solver
# ══════════════════════════════════════════════════════════════════════════════

def solve(
    kill_matrix:  List[List[bool]],
    mutants:      list,
    test_pool:    List[dict],
    verbose:      bool  = False,
    ilp_timeout:  float = 60.0,
) -> ILPResult:
    """
    Select the minimum test suite using ILP (CBC) with a Greedy fallback.

    Mutates mutant statuses IN-PLACE:
      - Mutants not killable by any test          → "SUSPECTED"
      - Coverable mutants killed by selected tests → "KILLED"
      - Coverable mutants not killed              → "ALIVE"

    Args:
        kill_matrix:  [n_tests × n_mutants] boolean matrix from Phase 3
        mutants:      list[MutantRecord] — .status modified in-place
        test_pool:    list of test dicts (used only for count display)
        verbose:      print solver details
        ilp_timeout:  CBC solver time limit in seconds

    Returns:
        ILPResult with scores, selected test indices, and suspected indices
    """
    n_tests   = len(kill_matrix)
    n_mutants = len(mutants)

    if n_tests == 0 or n_mutants == 0:
        return _empty_result(n_mutants)

    logger.info("[PHASE-4] ILP — selecting minimum test suite...")

    # Partition mutants into coverable vs. suspected
    coverable: List[int] = []
    suspected: List[int] = []

    for j in range(n_mutants):
        if any(kill_matrix[i][j] for i in range(n_tests)):
            coverable.append(j)
        else:
            suspected.append(j)
            mutants[j].status = "SUSPECTED"

    logger.info(f"  Coverable: {len(coverable)} | Suspected: {len(suspected)}")

    if not coverable:
        raw, adj = _calc_scores(0, n_mutants, len(suspected))
        return ILPResult(
            selected_tests=[],
            suspected_indices=suspected,
            raw_score=raw,
            adjusted_score=adj,
            n_killed=0,
            n_total=n_mutants,
            n_suspected=len(suspected),
            feasible=True,
        )

    # Select tests: ILP when PuLP is available, Greedy Set Cover otherwise.
    if pulp is None:
        logger.info(
            "  [WARN] PuLP not installed — using Greedy Set Cover fallback "
            "(install 'pulp' for an optimal minimum suite)"
        )
        selected = _greedy_fallback(kill_matrix, coverable, n_tests)
        feasible = False
    else:
        # Build and solve the ILP
        prob = pulp.LpProblem("MinTestSuite", pulp.LpMinimize)
        x    = [pulp.LpVariable(f"t{i}", cat="Binary") for i in range(n_tests)]

        prob += pulp.lpSum(x), "minimize_tests"

        for j in coverable:
            killers = [x[i] for i in range(n_tests) if kill_matrix[i][j]]
            if killers:
                prob += pulp.lpSum(killers) >= 1, f"cover_M{j}"

        solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=int(ilp_timeout))
        prob.solve(solver)

        status = pulp.LpStatus[prob.status]
        if verbose:
            logger.info(f"  ILP status: {status}")

        if prob.status == pulp.constants.LpStatusOptimal:
            selected = [
                i for i in range(n_tests)
                if x[i].varValue is not None and x[i].varValue > 0.5
            ]
            feasible = True
            if verbose:
                logger.info(f"  ILP optimal: {len(selected)} tests selected")
        else:
            if prob.status != pulp.constants.LpStatusInfeasible:
                logger.info(
                    f"  [WARN] ILP did not reach optimal in {ilp_timeout}s "
                    f"(status={status}) — Greedy fallback"
                )
            else:
                logger.info(f"  [WARN] ILP infeasible status — Greedy fallback")
            selected = _greedy_fallback(kill_matrix, coverable, n_tests)
            feasible = False

    # Update mutant statuses based on which are killed by selected tests
    for j in coverable:
        if any(
            i < len(kill_matrix)
            and j < len(kill_matrix[i])
            and kill_matrix[i][j]
            for i in selected
        ):
            mutants[j].status = "KILLED"
        else:
            mutants[j].status = "ALIVE"

    n_killed = _count_killed_by(kill_matrix, selected, coverable)
    raw, adj = _calc_scores(n_killed, n_mutants, len(suspected))

    logger.info(f"  Selected tests:  {len(selected)} (of {n_tests})")
    logger.info(f"  Raw Score:       {raw:.1f}%  ({n_killed}/{n_mutants})")
    logger.info(
        f"  Adjusted Score:  {adj:.1f}%  "
        f"({n_killed}/{n_mutants - len(suspected)})"
    )
    logger.info(f"[PHASE-4] ✓\n")

    return ILPResult(
        selected_tests=selected,
        suspected_indices=suspected,
        raw_score=raw,
        adjusted_score=adj,
        n_killed=n_killed,
        n_total=n_mutants,
        n_suspected=len(suspected),
        feasible=feasible,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Greedy Set Cover fallback
# ══════════════════════════════════════════════════════════════════════════════

def _greedy_fallback(
    kill_matrix: List[List[bool]],
    coverable:   List[int],
    n_tests:     int,
) -> List[int]:
    """
    Greedy Set Cover: repeatedly pick the test killing the most uncovered mutants.
    Guarantees an ln(n) approximation ratio.
    """
    uncovered: set = set(coverable)
    selected:  List[int] = []

    while uncovered:
        best_test  = -1
        best_count = -1

        for i in range(n_tests):
            count = sum(
                1 for j in uncovered
                if i < len(kill_matrix)
                and j < len(kill_matrix[i])
                and kill_matrix[i][j]
            )
            if count > best_count:
                best_count = count
                best_test  = i

        if best_test == -1 or best_count == 0:
            break

        selected.append(best_test)
        newly_covered = {
            j for j in uncovered
            if best_test < len(kill_matrix)
            and j < len(kill_matrix[best_test])
            and kill_matrix[best_test][j]
        }
        uncovered -= newly_covered

    return selected


# ══════════════════════════════════════════════════════════════════════════════
# Score tracking (used by the feedback loop for progress checks)
# ══════════════════════════════════════════════════════════════════════════════

def calc_current_score(
    kill_matrix:  List[List[bool]],
    mutants:      list,
    test_indices: Optional[List[int]] = None,
) -> Tuple[float, float, int]:
    """
    Compute the current mutation score without running ILP.

    Args:
        kill_matrix:  current kill matrix
        mutants:      list[MutantRecord] — reads .status field
        test_indices: which row indices to consider (None = all rows)

    Returns:
        (raw_score, adjusted_score, n_killed)
    """
    n_tests = len(kill_matrix)

    # Safely derive n_mutants even when mutants list is empty
    if mutants:
        n_mutants = len(mutants)
    elif kill_matrix and kill_matrix[0]:
        n_mutants = len(kill_matrix[0])
    else:
        return 0.0, 0.0, 0

    if test_indices is None:
        test_indices = list(range(n_tests))

    # Read suspected status from mutant objects if available
    if mutants:
        suspected_set = {
            j for j in range(n_mutants)
            if j < len(mutants) and mutants[j].status == "SUSPECTED"
        }
    else:
        suspected_set = set()

    coverable = [j for j in range(n_mutants) if j not in suspected_set]
    n_killed  = _count_killed_by(kill_matrix, test_indices, coverable)
    raw, adj  = _calc_scores(n_killed, n_mutants, len(suspected_set))
    return raw, adj, n_killed


# ══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _count_killed_by(
    kill_matrix:    List[List[bool]],
    test_indices:   List[int],
    mutant_indices: List[int],
) -> int:
    """Count mutants from mutant_indices killed by at least one test in test_indices."""
    return sum(
        1 for j in mutant_indices
        if any(
            i < len(kill_matrix)
            and j < len(kill_matrix[i])
            and kill_matrix[i][j]
            for i in test_indices
        )
    )


def _calc_scores(
    n_killed:    int,
    n_total:     int,
    n_suspected: int,
) -> Tuple[float, float]:
    """Compute raw and adjusted mutation scores."""
    raw       = (n_killed / n_total   * 100) if n_total   > 0 else 0.0
    effective = n_total - n_suspected
    adj       = (n_killed / effective * 100) if effective > 0 else 0.0
    return raw, adj


def _empty_result(n_mutants: int) -> ILPResult:
    """Return a zero-valued ILPResult for degenerate inputs."""
    return ILPResult(
        selected_tests=[],
        suspected_indices=[],
        raw_score=0.0,
        adjusted_score=0.0,
        n_killed=0,
        n_total=n_mutants,
        n_suspected=0,
        feasible=True,
    )


def _require_pulp() -> None:
    """Exit with a clear message if PuLP is not installed."""
    if pulp is None:
        logger.info("[ERROR] PuLP library is not installed.")
        logger.info("  Fix: pip install pulp")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# Standalone test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import copy as _copy
    import os as _os

    _os.sys.path.insert(
        0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    )

    _require_pulp()
    print("[TEST] ILP with sample 5×4 matrix...")

    from core.mutant_generator import MutantRecord  # noqa: E402

    km = [
        [True,  False, True,  False],
        [True,  True,  False, False],
        [False, True,  True,  False],
        [False, False, False, False],
        [False, False, False, True ],
    ]

    fake_mutants = [
        MutantRecord(
            id=f"M{i+1:03d}", category="ARITH",
            operator_name="Add_to_Sub", integration_op=None,
            function_name="f", line=1, original_line_text="",
            mutated_node_text="", original_op="Add", mutated_op="Sub",
            code="",
        )
        for i in range(4)
    ]

    result = solve(km, _copy.deepcopy(fake_mutants), [], verbose=True)
    print(result)
    print(f"  Selected tests: {result.selected_tests}")
    print(f"  Suspected:      {result.suspected_indices}")