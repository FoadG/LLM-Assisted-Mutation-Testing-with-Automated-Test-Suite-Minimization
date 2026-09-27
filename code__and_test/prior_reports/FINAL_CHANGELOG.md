# FINAL_CHANGELOG.md

All changes relative to the audited `/mnt/project/` snapshot.

## Fixed
- **core/container_executor.py** — in-container runner now serializes the function
  return value on success (`emit("OK", serialized)` using
  `json.dumps(result, default=str, sort_keys=True)` with a `repr` fallback),
  matching `core/sandbox_executor._execute_job`. Previously it emitted `("OK", None)`,
  so against `orig_results` (which carry the serialized output) the kill predicate
  `res != orig_results[i]` always fired — the container backend reported nearly every
  mutant killed regardless of behavior. Correctness fix; no signature change.
- **baseline.py** — `res.min_test_count` → `len(res.selected_tests)`. `ILPResult` has
  no `min_test_count` attribute; the value is derived in `core/reporter.py:123`. The
  original line would raise `AttributeError` if executed.
- **pyproject.toml** — `[tool.setuptools.packages.find].include` corrected to
  `["core*","utils*","strategies*","llm*","infra*","cli*"]`. The original omitted
  `utils*` (installed wheels dropped the `utils/` package) and listed `execution*`
  and `reporting*`, which do not exist (those modules live in `core/` and `utils/`).

## Added
- **main.py** (root orchestrator) — RECONSTRUCTED. The original collided with
  `cli/main.py` on the flattened disk and was lost. Rebuilt from verified evidence:
  pipeline order/signatures from `baseline.py`; Phase 5 from `core/feedback_loop.run`;
  CLI flags (`--config/--target/--no-llm/--verbose`) from the contract `gui.py` invokes;
  artifact writes via `core/reporter.generate` + `core/mutant_generator.save_mutants_json`;
  history via `utils/history_tracker.record_run`. Adds equivalence + feedback annotations.
- **control_center.py** + **control_center.html** — dependency-free (stdlib) web control
  center exposing the real-but-previously-unexposed capabilities (ProjectModel, import/call
  graphs, pre-run mutation preview + diff, equivalence, history/trend, environment, pytest
  export, backend selection) plus run launch/stop/stream. Every endpoint calls a verified
  function; unbacked features are listed read-only, never as controls.
- **`{core,utils,strategies,llm,infra,cli}/__init__.py`** — regular-package markers.
- **tests/golden/MISSING_FIXTURES.txt** — documents the absent V1 golden fixtures.
- Reports: this changelog plus AUDIT / TREE / INTEGRATION / GUI / DEPENDENCY / CONSISTENCY.

## Removed
- **sandbox_executor_.py** — 0-byte orphan; no importer.
- top-level **__init__.py** — root is not a package.

## Relocated (content unchanged)
- All remaining modules moved from the flat directory into their declared package homes
  (`core/`, `utils/`, `strategies/`, `llm/`, `infra/`, `cli/`, `tests/`). Verified by
  importer references and per-file headers; see FINAL_REPOSITORY_AUDIT.md.

## Known gaps (not changed, flagged)
- `tests/golden/*` fixtures are absent from the snapshot and were **not** fabricated;
  `tests/test_golden.py::test_golden_fixtures_present_and_valid` will error until restored.
- The reconstructed `main.py` is verified to import/compile and to drive Phases 1–2 to
  48 mutants (matching the documented golden `total_mutants=48`); full end-to-end (P3 worker
  pool + P4 ILP needing `pulp`) was not executed in the build sandbox.
