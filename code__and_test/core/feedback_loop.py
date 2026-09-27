"""
core/feedback_loop.py — Phase 5
=================================
Targeted feedback loop for surviving (ALIVE) mutants.

The loop asks the LLM to generate test inputs specifically designed to kill
mutants that survived all previous phases.  It runs for at most
`feedback_max_rounds` iterations, stopping early when the target score is
reached or no progress is made.

IMPORTANT: `run()` returns (kill_matrix, orig_results, ilp_result).
The caller must capture all three to keep orig_results in sync with
all_tests after new tests are added during feedback rounds.
"""

from __future__ import annotations

from typing import List, Tuple

from core import sandbox_executor, ilp_solver
from core.ilp_solver import ILPResult
from utils.json_utils import safe_parse_json, deduplicate, test_hash
import logging

logger = logging.getLogger(__name__)


def run(
    mutants:       list,
    kill_matrix:   List[List[bool]],
    orig_results:  List[tuple],
    all_tests:     List[dict],
    original_code: str,
    ilp_result:    ILPResult,
    llm_client:    object,
    config:        dict,
) -> Tuple[List[List[bool]], List[tuple], ILPResult]:
    """
    Execute the targeted feedback loop.

    Mutates in-place:
      - mutants[*].status      (via ilp_solver.solve each round)
      - all_tests              (extended with new tests via list.extend)

    Args:
        mutants:       list[MutantRecord] — statuses modified in-place
        kill_matrix:   current kill matrix (may be extended)
        orig_results:  original-code results per test (may be extended)
        all_tests:     full test pool (extended in-place with new tests)
        original_code: source string for the original target file
        ilp_result:    ILPResult from Phase 4
        llm_client:    object with .call(prompt, temperature) -> str
        config:        full config.json dict

    Returns:
        (kill_matrix, orig_results, ilp_result)
        All three values reflect any extensions made during the loop.
    """
    opt         = config.get("optimizer", {})
    target      = float(opt.get("target_score",                      90.0))
    max_rounds  = int(opt.get("feedback_max_rounds",                  3))
    max_muts    = int(opt.get("feedback_max_mutants_per_round",       10))
    max_new     = int(opt.get("feedback_max_new_tests_per_mutant",    2))
    failed_ctx  = int(opt.get("feedback_failed_inputs_in_prompt",     3))

    current_result = ilp_result

    logger.info("[PHASE-5] Targeted feedback loop...")
    logger.info(f"  Target: {target}% | Max rounds: {max_rounds}")

    for round_num in range(1, max_rounds + 1):
        score = current_result.adjusted_score

        if score >= target:
            logger.info(f"  [Round {round_num}] Score={score:.1f}% >= {target}% → stop ✓")
            break

        alive = [m for m in mutants if m.status == "ALIVE"]
        if not alive:
            logger.info(f"  [Round {round_num}] No surviving mutants → stop")
            break

        logger.info(
            f"\n  [Round {round_num}] Score={score:.1f}% | "
            f"Alive: {len(alive)} | Target: {target}%"
        )

        prev_killed            = current_result.n_killed
        new_tests: List[dict] = []

        for mutant in alive[:max_muts]:
            fn            = mutant.function_name
            failed_sample = mutant.tried[-failed_ctx:] if mutant.tried else []

            prompt = _build_feedback_prompt(mutant, failed_sample, fn)
            raw    = llm_client.call(prompt, temperature=0.95)  # type: ignore[union-attr]

            if not raw:
                continue

            parsed = safe_parse_json(raw)
            if not parsed:
                continue

            for item in parsed[:max_new]:
                if not isinstance(item, dict):
                    continue
                new_tests.append({"function": fn, "inputs": item})
                mutant.tried.append(item)

        if not new_tests:
            logger.info(f"  [Round {round_num}] LLM produced no new tests → stop")
            break

        # Deduplicate and filter against the existing pool
        new_tests = deduplicate(new_tests)
        existing  = {test_hash(t) for t in all_tests}
        new_tests = [t for t in new_tests if test_hash(t) not in existing]

        if not new_tests:
            logger.info(f"  [Round {round_num}] All new tests were duplicates → stop")
            break

        # Extend kill matrix and orig_results together
        kill_matrix, orig_results = sandbox_executor.extend_kill_matrix(
            kill_matrix=kill_matrix,
            orig_results=orig_results,
            original_code=original_code,
            mutants=mutants,
            new_tests=new_tests,
            config=config,
        )
        all_tests.extend(new_tests)  # mutate in-place so caller sees the update

        logger.info(f"  [Round {round_num}] Added {len(new_tests)} new tests")

        current_result = ilp_solver.solve(
            kill_matrix=kill_matrix,
            mutants=mutants,
            test_pool=all_tests,
            verbose=False,
        )

        new_killed = current_result.n_killed
        logger.info(
            f"  [Round {round_num}] Killed: {prev_killed} → {new_killed} | "
            f"Score: {current_result.adjusted_score:.1f}%"
        )

        if new_killed <= prev_killed:
            logger.info(f"  [Round {round_num}] No progress → stop")
            break

    # Convert all remaining ALIVE mutants to SUSPECTED
    n_converted = 0
    for m in mutants:
        if m.status == "ALIVE":
            m.status = "SUSPECTED"
            n_converted += 1

    if n_converted:
        logger.info(f"\n  [PHASE-5] {n_converted} surviving mutant(s) → SUSPECTED")

    emoji = "✓" if current_result.adjusted_score >= target else "△"
    logger.info(
        f"[PHASE-5] {emoji} Final score: "
        f"{current_result.adjusted_score:.1f}% | "
        f"Min tests: {len(current_result.selected_tests)}\n"
    )

    return kill_matrix, orig_results, current_result


# ══════════════════════════════════════════════════════════════════════════════
# Prompt builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_feedback_prompt(
    mutant,
    failed_sample: list,
    func_name:     str,
) -> str:
    """Build a targeted prompt for a mutant that has not been killed yet."""
    mut_lines = mutant.code.splitlines()
    mut_line  = ""
    if 1 <= mutant.line <= len(mut_lines):
        mut_line = mut_lines[mutant.line - 1].strip()

    failed_str = str(failed_sample) if failed_sample else "none yet"

    return (
        f"You are a precise software tester. This mutant has NOT been killed yet.\n\n"
        f"Function: {func_name}\n"
        f"Original line {mutant.line}: {mutant.original_line_text.strip()}\n"
        f"Mutated  line {mutant.line}: {mut_line}\n\n"
        f"These inputs did NOT expose the difference (do NOT repeat them):\n"
        f"{failed_str}\n\n"
        f"Task: Generate ONE completely different input dict for '{func_name}' "
        f"that produces a DIFFERENT output for the original vs the mutated code.\n\n"
        f"Return ONLY a valid JSON dict. No explanation, no markdown.\n"
        f"Example: {{\"arr\": [5, 3, 1, 2]}}"
    )