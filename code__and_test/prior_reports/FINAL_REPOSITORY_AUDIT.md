# FINAL_REPOSITORY_AUDIT.md

Re-derived from scratch (prior outputs were re-verified, not trusted). The audited
input at `/mnt/project/` was a **flattened** snapshot (all modules in one directory);
package homes were reconstructed from each file's declared header **and** confirmed by
how importers reference them (e.g. `from utils.config_loader import load_config`).

Legend: **UNCHANGED** = byte-identical content, only relocated to its package home ·
**UPDATED** = content corrected · **NEW** = newly authored · **REMOVED** = dropped.

## core/
| File | Status | Evidence |
|---|---|---|
| core/contracts.py | UNCHANGED | header `core/contracts.py`; imported as `core.contracts` |
| core/mutant_generator.py | UNCHANGED | header `core/mutant_generator.py`; `from core.mutant_generator import …` |
| core/sandbox_executor.py | UNCHANGED | header `core/sandbox_executor.py` |
| core/executor.py | UNCHANGED | header `core/executor.py` |
| **core/container_executor.py** | **UPDATED** | ISSUE-1 fix: in-container `OK` now serializes the return value (`emit("OK", serialized)`), matching `_execute_job`. Without it the kill predicate `res != orig_results[i]` mis-fires under the container backend. Verified present: `grep -c 'emit("OK", serialized)' == 1`. |
| core/ilp_solver.py | UNCHANGED | header `core/ilp_solver.py` |
| core/feedback_loop.py | UNCHANGED | header `core/feedback_loop.py` |
| core/equivalence.py | UNCHANGED | header `core/equivalence.py` |
| core/project_model.py | UNCHANGED | header `core/project_model.py`; imports `from core import graphs` |
| core/graphs.py | UNCHANGED | header `core/graphs.py` |
| core/identity.py | UNCHANGED | header `core/identity.py` |
| core/cell_cache.py | UNCHANGED | header `core/cell_cache.py`; imports `core.identity` |
| core/test_pool_builder.py | UNCHANGED | header `core/test_pool_builder.py`; `from core.test_pool_builder import build` (baseline.py) |
| core/reporter.py | UNCHANGED | header `core/reporter.py` (NOT `reporting/`) |
| core/feedback_analysis.py | UNCHANGED | header `core/feedback_analysis.py` |
| core/history_readback.py | UNCHANGED | header `core/history_readback.py` |
| core/run_context.py | UNCHANGED | header `core/run_context.py` |

## utils/
| File | Status | Evidence |
|---|---|---|
| utils/config_loader.py | UNCHANGED | header `utils/config_loader.py`; `from utils.config_loader import load_config` |
| utils/json_utils.py | UNCHANGED | header `utils/json_utils.py`; `from utils.json_utils import …` |
| utils/history_tracker.py | UNCHANGED | header `utils/history_tracker.py`; `from utils.history_tracker import …` |
| utils/cache_manager.py | UNCHANGED | header `utils/cache_manager.py` |
| utils/coverage_filter.py | UNCHANGED | header `utils/coverage_filter.py` (NOT `core/` — corrects a prior assumption) |
| utils/pytest_exporter.py | UNCHANGED | header `utils/pytest_exporter.py` (NOT `reporting/` — corrects a prior assumption) |

## strategies/  ·  llm/  ·  infra/
| File | Status | Evidence |
|---|---|---|
| strategies/{base,registry,acoc_strategy,llm_strategy,domain_strategy}.py | UNCHANGED | headers `strategies/…`; `from strategies.registry import get_enabled` |
| llm/client.py | UNCHANGED | header `llm/client.py`; `from llm.client import LLMClient` |
| infra/{logging_setup,manifest,reproducibility}.py | UNCHANGED | headers `infra/…`; `from infra.reproducibility import capture_env` (test) |

## cli/ · tests/ · top-level
| File | Status | Evidence |
|---|---|---|
| cli/main.py | UNCHANGED | the flattened `main.py` self-identifies as `cli/main.py` ("Thin CLI entry point"); `import main; main.main()` |
| **main.py** (root orchestrator) | **NEW (RECONSTRUCTED)** | original absent (collided with `cli/main.py` during flattening). Rebuilt strictly from `baseline.py` (P1–P4 order/signatures), `feedback_loop.run` (P5), `gui.py` CLI contract, `reporter.generate`/`save_mutants_json` artifact writes, `record_run` fields. Not a byte-recovery of the original. |
| **baseline.py** | **UPDATED** | `res.min_test_count` → `len(res.selected_tests)`: `ILPResult` has no `min_test_count` attribute (it is derived in `core/reporter.py:123`). Original line was a latent `AttributeError`. |
| target_code.py | UNCHANGED | top-level py-module |
| verify.py | UNCHANGED | top-level py-module (self-test harness) |
| gui.py | UNCHANGED | top-level py-module (existing Flask GUI) |
| microbench.py | UNCHANGED | dev benchmark; `sys.path.insert(0,'.')` |
| config.json | UNCHANGED | matches `config_loader.REQUIRED_TOP_KEYS` |
| **pyproject.toml** | **UPDATED** | `packages.find.include` corrected to the six real packages — original omitted `utils*` (installed wheels would drop `utils/`) and listed non-existent `execution*`/`reporting*`. |
| tests/{test_golden,test_no_prints,test_reproducibility}.py | UNCHANGED | headers `tests/…` |
| **control_center.py** | **NEW** | stdlib control center; re-verification fixed its imports to `utils.pytest_exporter` and `infra.reproducibility` (prior fallbacks targeted non-existent `reporting.*`). |
| **control_center.html** | **NEW** | single-file frontend; every control maps to a wired endpoint |
| `{core,utils,strategies,llm,infra,cli}/__init__.py` | **NEW** | regular-package markers required by the reconstructed layout |
| tests/golden/MISSING_FIXTURES.txt | **NEW (note)** | documents 3 golden fixtures absent from the snapshot (not fabricated) |

## REMOVED
| File | Status | Evidence |
|---|---|---|
| sandbox_executor_.py | **REMOVED** | 0 bytes — orphan/placeholder; nothing imports it |
| top-level `__init__.py` | **REMOVED** | repo root is not a package (top-level modules are `py-modules`); replaced by per-package `__init__.py` |

## Cannot be supplied (UNKNOWN — not fabricated)
- `tests/golden/mutation_report_v1.json`, `minimum_test_suite_v1.json`, `baseline_runtime_seconds.txt` — original V1 captures; absent from the snapshot. See `tests/golden/MISSING_FIXTURES.txt`.
