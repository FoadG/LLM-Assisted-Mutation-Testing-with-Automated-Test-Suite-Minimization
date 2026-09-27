"""
main.py — Pipeline Orchestrator (root entry, M6 placeholder)
=============================================================
Sequences the six pipeline phases for a single target file:

  P1  Generate mutants        core.mutant_generator.generate_all_mutants
  P2  Build test pool         core.test_pool_builder.build
  P3  Build kill matrix       core.sandbox_executor.build_kill_matrix
  P4  Select minimum suite    core.ilp_solver.solve
  P5  Feedback loop (LLM)      core.feedback_loop.run        (skipped with --no-llm)
  P6  Report                  core.reporter.generate        (writes output/*.json)

This module owns sequencing only — no business logic lives here. It logs via
``logging.getLogger(__name__)`` (no ``print``) and configures stdout logging
through ``infra.logging_setup`` so the human-readable console look is preserved
(the GUI scrapes ``[PHASE-N]`` lines from stdout). It is import-guarded under
``if __name__ == "__main__":`` because Phase-3 uses ``multiprocessing`` spawn,
which re-imports the entry module in every worker.

CLI (the contract the Flask GUI relies on):
    python main.py [--no-llm] [--verbose] [--json-logs] [--config CONFIG]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from infra.logging_setup import configure
from infra.manifest import Manifest, validate as validate_manifest
from infra.reproducibility import DEFAULT_ACOC_SEED, capture_env
from utils.config_loader import load_config

logger = logging.getLogger(__name__)


class _DummyLLM:
    """Offline LLM stand-in used for --no-llm runs.

    Satisfies the ``.call(prompt, temperature)`` contract every consumer uses
    (test_pool_builder/llm_strategy/feedback_loop). Returns an empty JSON list,
    so ``safe_parse_json`` yields no tests and the pipeline runs deterministically
    with no network access.
    """

    def call(self, prompt: str, temperature: float = 0.0) -> str:  # noqa: D401
        return "[]"


def _make_llm_client(config: dict, no_llm: bool):
    """Return a real client only when LLM is both requested and enabled."""
    llm_enabled = bool(config.get("test_pool", {}).get("llm_enabled", False))
    if no_llm or not llm_enabled:
        return _DummyLLM()
    # Real client only on the explicit, enabled path (requires ANTHROPIC_API_KEY).
    from llm.client import LLMClient
    return LLMClient(config.get("llm", {}))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="LLM-assisted mutation-testing pipeline (single target file).",
    )
    p.add_argument("--config", default="config.json",
                   help="path to config.json (default: config.json)")
    p.add_argument("--no-llm", action="store_true",
                   help="disable all LLM calls; fully offline/deterministic run")
    p.add_argument("--verbose", action="store_true",
                   help="verbose per-phase logging")
    p.add_argument("--json-logs", action="store_true",
                   help="emit structured JSON log lines instead of plain text")
    p.add_argument("--workers", type=int, default=None, metavar="N",
                   help="parallel mutant-execution workers (Phase-3 pool). "
                        "Default: config.sandbox.parallel_workers, else min(4, cpu). "
                        "Performance only — results are worker-count-independent.")
    return p.parse_args(argv)


def run_pipeline(config: dict, no_llm: bool, verbose: bool) -> int:
    """Execute P1-P6 for the single configured target file.

    Returns a process exit code (0 = completed).
    """
    # Imported here (after logging is configured and inside the __main__ guard's
    # call path) so spawn workers re-importing this module do not trigger work.
    from core.mutant_generator import generate_all_mutants
    from core.test_pool_builder import build as build_pool
    from core.sandbox_executor import build_kill_matrix
    from core.ilp_solver import solve
    from core import feedback_loop, reporter
    from core import equivalence, feedback_analysis  # M2: I4 + I6 (advisory)
    from core.project_model import build_project_model  # N1.6: qualified attribution

    project = config.get("project", {})
    target_file = project.get("target_file", "target_code.py")
    output_dir = project.get("output_dir", "output")

    with open(target_file, encoding="utf-8") as f:
        source_code = f.read()

    # N1.6: build the queryable project model once in the parent process. For a
    # single-file config this is a one-module model (dotted = target_file stem);
    # it is used for OBSERVATIONAL qualified attribution only and is never passed
    # to spawn workers.
    _project_model = build_project_model(config)
    _module_name = os.path.splitext(os.path.basename(target_file))[0]

    llm_client = _make_llm_client(config, no_llm)

    # I2/N20: run manifest with reproducibility envelope attached. Purely
    # observational — records phase timings/counts; never alters pipeline data.
    manifest = Manifest(envelope=capture_env(DEFAULT_ACOC_SEED))
    manifest.event("run_start", target_file=target_file, no_llm=bool(no_llm))

    # ── P1: mutants ──────────────────────────────────────────────────────────
    with manifest.phase("P1_generate"):
        mutants = generate_all_mutants(source_code, config.get("mutation", {}),
                                       verbose=verbose,
                                       project_model=_project_model,
                                       module_name=_module_name)
    manifest.event("mutants_generated", count=len(mutants))
    if not mutants:
        logger.warning("No mutants generated — nothing to do.")
        return 0

    # ── P2: test pool ────────────────────────────────────────────────────────
    with manifest.phase("P2_pool"):
        all_tests = build_pool(mutants, llm_client, config)
    manifest.event("pool_built", count=len(all_tests))

    # ── P3: kill matrix ──────────────────────────────────────────────────────
    with manifest.phase("P3_matrix", tests=len(all_tests), mutants=len(mutants)):
        kill_matrix, orig_results = build_kill_matrix(
            source_code, mutants, all_tests, config, verbose=verbose
        )

    # ── P4: minimum suite selection ──────────────────────────────────────────
    with manifest.phase("P4_select"):
        ilp_result = solve(kill_matrix, mutants, all_tests, verbose=verbose)

    # ── P5: targeted feedback loop (LLM only) ────────────────────────────────
    target_score = float(config.get("optimizer", {}).get("target_score", 90.0))
    if (not no_llm) and isinstance(llm_client, _DummyLLM) is False \
            and ilp_result.adjusted_score < target_score:
        with manifest.phase("P5_feedback"):
            kill_matrix, orig_results, ilp_result = feedback_loop.run(
                mutants, kill_matrix, orig_results, all_tests,
                source_code, ilp_result, llm_client, config,
            )
    else:
        logger.info("[PHASE-5] Skipped (LLM disabled or target already met).")

    # ── M2: advisory analysis over results (I4 equivalence + I6 feedback) ────
    # Read-only; never mutates statuses or the V1 scoring path.
    equivalent_ids = equivalence.detect_equivalent(mutants, source_code)
    equivalence_summary = equivalence.summarize(mutants, ilp_result, equivalent_ids)
    feedback = feedback_analysis.analyze(mutants, ilp_result, equivalent_ids)
    manifest.event("equivalence", count=equivalence_summary["count"])
    manifest.event("feedback", survivors=feedback["survivors"])

    # ── P6: report ───────────────────────────────────────────────────────────
    with manifest.phase("P6_report"):
        reporter.generate(
            mutants, all_tests, kill_matrix, ilp_result,
            source_code, config, orig_results,
            equivalence=equivalence_summary,   # I7: additive analysis section
            feedback=feedback,
        )

    manifest.event(
        "result",
        killed=ilp_result.n_killed, total=ilp_result.n_total,
        adjusted_score=ilp_result.adjusted_score,
        selected_tests=len(ilp_result.selected_tests),
    )

    # I2: emit + validate the run manifest (additive artifact; does not touch
    # mutation_report.json / minimum_test_suite.json).
    manifest_path = os.path.join(output_dir, "run_manifest.json")
    manifest.finalize(manifest_path)
    errors = validate_manifest(manifest.to_dict())
    if errors:
        logger.warning("[MANIFEST] schema validation failed: %s", errors)
    else:
        logger.info("[MANIFEST] %s (schema v%d, run %s)",
                    manifest_path, manifest.to_dict()["schema_version"],
                    manifest.run_id)

    logger.info(
        "[DONE] killed=%d/%d | adjusted=%.1f%% | suite=%d test(s)",
        ilp_result.n_killed, ilp_result.n_total,
        ilp_result.adjusted_score, len(ilp_result.selected_tests),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure(level=logging.DEBUG if args.verbose else logging.INFO,
              json_mode=args.json_logs)
    config = load_config(args.config, non_interactive=True)
    # M3/I1: optional CLI override for the Phase-3 worker pool. Affects only
    # concurrency (performance), never the deterministic index-keyed result.
    if args.workers is not None:
        config.setdefault("sandbox", {})["parallel_workers"] = max(1, args.workers)
    return run_pipeline(config, no_llm=args.no_llm, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
