# FINAL_DEPENDENCY_REPORT.md

## External dependencies (declared + how used)
| Package | Required? | Used by | Behavior if missing |
|---|---|---|---|
| `pulp` (>=2.7) | runtime (ILP) | `core/ilp_solver.py` (`_require_pulp`, lazy) | greedy fallback / clear error at solve time; import of the module is safe |
| `coverage` (>=7.0) | optional | `utils/coverage_filter.py` (lazy `import coverage`) | function returns `None` with a clear log line; never crashes import |
| `pytest` (>=7.0) | dev/test | `tests/*` | only affects running tests |
| `flask` | optional (GUI) | `gui.py` | `gui.py` prints an install hint and exits; **control_center.py needs no third-party deps** |
| stdlib only | — | `control_center.py`, everything else at import time | — |

The Anthropic API is reached by `llm/client.py` via `urllib` (stdlib) using
`ANTHROPIC_API_KEY`; `--no-llm` / `DummyLLM` avoids all network use.

## Internal import graph (verified by executed imports — 36/36 OK)
Key edges (importer → imported):
- `core/*` depend on `core.contracts` (stdlib-only domain core; imports no sibling).
- `core/project_model` → `core.graphs`.
- `core/cell_cache` → `core.identity`.
- `core/feedback_loop` → `core.sandbox_executor`, `core.ilp_solver`, `utils.json_utils`.
- `core/test_pool_builder` → `strategies.base`, `strategies.registry`, `utils.json_utils`.
- `strategies/registry` → `strategies.{acoc,llm,domain}_strategy`, `strategies.base`.
- `strategies/llm_strategy` → `utils.json_utils`.
- `core/reporter`, `core/feedback_analysis`, `core/equivalence` → `core.contracts` / `core.ilp_solver`.
- `core/history_readback` → `utils.history_tracker`.
- `infra/reproducibility` ← `tests/test_reproducibility`.
- `cli/main` → `main` (root orchestrator) at call time.
- `main` (root) → `infra.logging_setup`, `utils.config_loader`, `core.{mutant_generator,
  test_pool_builder,sandbox_executor,ilp_solver,feedback_loop,equivalence,feedback_analysis,
  reporter}`, `utils.history_tracker`, `llm.client` (lazy, non-`--no-llm` only).

## Lazy cycles (intentional, verified safe)
- `core.executor` ↔ `core.sandbox_executor`: each imports the other only **inside functions**
  (`make_executor`/`_build_columns_via_pool` and `WorkerPoolExecutor.start`/`LegacyExecutor.submit_many`).
  No import-time cycle — both import cleanly.
- `core.executor` → `core.container_executor`: lazy, only when `backend == "container"`.

## Configuration dependency graph
`config.json` top keys → consumers:
`project.{output_dir,target_file,root,discovery}` → reporter / project_model;
`functions, partitions` → strategies / config_loader validation;
`mutation.*` → mutant_generator; `test_pool.{acoc_max_per_function,llm_*,strategies}` →
strategies/registry; `sandbox.{execution_backend,timeout_seconds,parallel_workers,
timeout_floor_seconds,timeout_multiplier}` → executor/sandbox_executor; `optimizer.*` →
feedback_loop; `llm.{model,max_tokens,api_base}` → llm.client.

## Missing / unresolved
None at import time. Only unresolved artifacts are the absent `tests/golden/*` fixtures
(data, not code) — documented, not fabricated.
