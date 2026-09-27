# FINAL_GUI_REPORT.md

Two GUIs ship: the existing **gui.py** (Flask) and the new **control_center.py** (stdlib).
Every control is mapped to a real backend below. No dead controls, no placeholders.

## gui.py (Flask) — existing, unchanged
| Control / route | Backend |
|---|---|
| `GET /` | serves embedded HTML |
| `GET/POST /api/config` | read / write `config.json` |
| `GET/POST /api/target` | read / write `target_code.py` |
| `POST /api/run` | `subprocess` → `python main.py --config --target [--no-llm] [--verbose]` |
| `POST /api/stop` | `proc.kill()` |
| `GET /api/status` | run-state dict |
| `GET /api/stream` | SSE log stream |
| `GET /api/results/{mutants,report,suite}` | reads `output/*.json` |
| `GET /api/results/download/<f>` | `send_file` (allowlisted names) |
| `GET/POST /api/env/apikey` | `ANTHROPIC_API_KEY` env |

Dependency note: `gui.py` launches `main.py` and reads `output/{mutants,mutation_report,minimum_test_suite}.json`. The reconstructed `main.py` writes exactly those three files (`reporter.generate` + `save_mutants_json`), so the GUI's read-back contract is satisfied.

## control_center.py (stdlib) — new
Verified in-process (see FINAL_INTEGRATION_REPORT.md Step 9). Each control → endpoint → verified function:

| UI control | Endpoint | Verified backend call |
|---|---|---|
| Overview · environment | `GET /api/env` | `infra.reproducibility.capture_env` |
| Overview · backends | `GET /api/backends` | config `sandbox.execution_backend` → `core.executor.make_executor` |
| Configuration · Reload/Validate | `GET /api/config` | `utils.config_loader.load_config(non_interactive=True)` |
| Configuration · Save changes | `POST /api/config` | `json.dump` |
| Project · Build model | `GET /api/project/model` | `core.project_model.build_project_model` |
| Project · row → symbols | `GET /api/project/symbols` | `ProjectModel.symbols(module)` |
| Graphs · import | `GET /api/graph/import` | `core.graphs.build_import_graph` via `ProjectModel.import_graph()` |
| Graphs · call | `GET /api/graph/call` | `core.graphs.build_call_graph` via `ProjectModel.call_graph()` |
| Mutation · Preview mutants | `POST /api/mutation/preview` | `core.mutant_generator.generate_all_mutants` |
| Mutation · row → diff | `GET /api/mutation/diff` | `MutantRecord.code` vs target source |
| Run · Launch / Stop | `POST /api/run` · `/api/run/stop` | `subprocess` (mirrors gui.py) |
| Run · live log | `GET /api/run/stream` | SSE from log queue |
| Run · Export pytest suite | `POST /api/export/pytest` | `utils.pytest_exporter.export_from_file` |
| Results · report/mutants/suite | `GET /api/results/*` | `output/*.json` (written by `core.reporter.generate`) |
| Results · Detect equivalents | `GET /api/results/equivalence` | `core.equivalence.detect_equivalent` |
| History · Load history | `GET /api/history/{trend,runs}` | `utils.history_tracker.load_history/get_trend` |
| Capabilities | `GET /api/capabilities` | static map (wired vs not-supported) |

## Controls deliberately ABSENT (no backend — shown only as read-only reference)
Pause/resume/restart; run-only/skip a phase; live worker/queue/thread telemetry; LLM
token/cost; execution replay; experiment/scenario/batch/scheduler; multi-project; mutant
lineage; live CPU/RAM/disk. Each appears in the "Not supported (evidence)" panel — never
as an interactive control — because the repository has no code to back it. Evidence per item
is served by `/api/capabilities` and listed in the project README.
