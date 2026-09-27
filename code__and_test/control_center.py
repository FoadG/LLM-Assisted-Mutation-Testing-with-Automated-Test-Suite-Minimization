"""
control_center.py — V3 Control Center (Inspector + Pipeline console)
====================================================================
A dependency-free (Python standard library only) web control center for the
mutation-testing framework.

WHY THIS EXISTS / SCOPE (read before extending)
------------------------------------------------
The repo already ships a Flask GUI (gui.py) that exposes config edit, target
edit, run launch/stop, log stream, and post-run result viewing. This control
center is ADDITIVE: it exposes the REAL capabilities that gui.py does NOT —
ProjectModel / module explorer, import graph, call graph, pre-run mutation
preview + operator statistics + source diff, provable-equivalence detection,
run history + trend, environment/reproducibility capture, and pytest export —
and it does so with zero third-party dependencies (stdlib http.server).

INTEGRITY RULE ENFORCED IN CODE
-------------------------------
Every endpoint calls a VERIFIED function from the repository by its real
dotted path (the same paths the repo's own modules import, e.g.
`from core.project_model import build_project_model`). No endpoint fabricates
data. If a backing module/dependency is unavailable, the endpoint returns an
explicit error — it never returns fake/placeholder results.

CONTROLS DELIBERATELY NOT IMPLEMENTED (no backend in this repo — see /api/capabilities):
  pause/resume/skip-phase, run-only-selected-phase, live worker/queue/thread
  telemetry, LLM token/cost accounting, execution replay, experiment tracking,
  scenario/batch/scheduler, multi-project management, mutant lineage.
  These are reported as NOT_SUPPORTED with evidence rather than shipped as dead
  controls.

PLACEMENT / RUNTIME
-------------------
Place this file at the PROJECT ROOT (next to main.py / gui.py / config.json),
where the `core`, `utils`, `strategies` packages are importable. Then:

    python control_center.py            # serves http://127.0.0.1:8770
    python control_center.py --port 9000 --host 0.0.0.0

Static verification only was possible in the authoring sandbox (the audited
copy was flattened, so the package layout could not be exercised at runtime).
Signatures called here were read directly from the source and are listed in
README.md alongside their evidence.
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
HTML_PATH = Path(__file__).resolve().parent / "control_center.html"

# Make the project root importable for `core.*`, `utils.*`, `strategies.*`.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
class ApiError(Exception):
    """Raised by endpoint logic; carries an HTTP status code."""

    def __init__(self, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.status = status


def _imp(dotted: str):
    """Import a repo module by its real dotted path, or raise a clean ApiError.

    This is the single seam that ties the control center to the codebase. If the
    package layout is not present (e.g. running from a flattened copy), the
    caller gets an explicit, non-faked error."""
    try:
        return importlib.import_module(dotted)
    except Exception as exc:  # ImportError and anything raised at import time
        raise ApiError(
            f"cannot import '{dotted}': {type(exc).__name__}: {exc} "
            f"(place control_center.py at the project root with the core/utils/"
            f"strategies packages importable)",
            status=503,
        )


def _read_config() -> dict:
    if not CONFIG_PATH.exists():
        raise ApiError(f"config.json not found at {CONFIG_PATH}", status=404)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise ApiError(f"config.json is not valid JSON: {exc}", status=400)


def _out_dir(config: dict) -> str:
    return (config.get("project", {}) or {}).get("output_dir", "output/")


def _target_path(config: dict) -> Path:
    tf = (config.get("project", {}) or {}).get("target_file", "target_code.py")
    p = Path(tf)
    return p if p.is_absolute() else (PROJECT_ROOT / tf)


# ──────────────────────────────────────────────────────────────────────────────
# Run launcher (mirrors gui.py's verified subprocess pattern)
# ──────────────────────────────────────────────────────────────────────────────
_run_lock = threading.Lock()
_run_state: dict = {
    "running": False, "phase": None, "started_at": None,
    "ended_at": None, "exit_code": None, "args": [],
}
_process: subprocess.Popen | None = None
_log_queue: "queue.Queue[dict]" = queue.Queue(maxsize=4000)


def _launch_pipeline(no_llm: bool, verbose: bool, backend: str | None) -> None:
    """Launch `python main.py --config ... --target ...` exactly as gui.py does.

    NOTE: the orchestrator `main` module is the launch target of the existing
    gui.py too; its CLI flags (--config/--target/--no-llm/--verbose) are the
    contract gui.py already assumes. If `backend` is given, it is written into
    config.sandbox.execution_backend first (a VERIFIED key consumed by
    core.executor.make_executor)."""
    global _process
    config = _read_config()
    if backend:
        sb = config.setdefault("sandbox", {})
        sb["execution_backend"] = backend
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    target = _target_path(config)
    cmd = [sys.executable, str(PROJECT_ROOT / "main.py"),
           "--config", str(CONFIG_PATH), "--target", str(target)]
    if no_llm:
        cmd.append("--no-llm")
    if verbose:
        cmd.append("--verbose")

    with _run_lock:
        _run_state.update(running=True, phase="starting",
                          started_at=datetime.now().isoformat(),
                          ended_at=None, exit_code=None, args=cmd)
    while not _log_queue.empty():
        try:
            _log_queue.get_nowait()
        except queue.Empty:
            break
    _log_queue.put({"type": "start", "text": "▶ " + " ".join(cmd)})

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    exit_code = -1
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, env=env,
        )
        with _run_lock:
            _process = proc
        import re
        for line in proc.stdout:  # type: ignore[union-attr]
            s = line.rstrip()
            if not s:
                continue
            mtype = "log"
            if "[ERROR]" in s or "Error" in s:
                mtype = "error"
            elif "[WARN]" in s:
                mtype = "warn"
            elif "[PHASE-" in s:
                mtype = "phase"
                m = re.search(r"\[PHASE-(\d+)\]", s)
                if m:
                    with _run_lock:
                        _run_state["phase"] = f"PHASE-{m.group(1)}"
            try:
                _log_queue.put_nowait({"type": mtype, "text": s})
            except queue.Full:
                pass
        proc.wait()
        exit_code = proc.returncode
    except Exception as exc:
        _log_queue.put({"type": "error", "text": f"[control_center] {exc}"})
    finally:
        with _run_lock:
            _run_state.update(running=False, phase=None,
                              ended_at=datetime.now().isoformat(),
                              exit_code=exit_code)
            _process = None
        _log_queue.put({"type": "done", "text": f"▪ finished (exit={exit_code})"})


# ──────────────────────────────────────────────────────────────────────────────
# Endpoint implementations — each wires ONE verified capability
# ──────────────────────────────────────────────────────────────────────────────
def ep_health(_q: dict) -> dict:
    return {
        "ok": True,
        "project_root": str(PROJECT_ROOT),
        "config_present": CONFIG_PATH.exists(),
        "python": sys.version.split()[0],
        "time": datetime.now().isoformat(timespec="seconds"),
    }


def ep_env(_q: dict) -> dict:
    # Verified path: infra/reproducibility.py (header + importers + test).
    repro = _imp("infra.reproducibility")
    return {"ok": True, "data": repro.capture_env()}


def ep_config_get(_q: dict) -> dict:
    raw = _read_config()
    # Validate against the REAL schema (config_loader.load_config, non-interactive).
    loader = _imp("utils.config_loader")
    validated, error = None, None
    try:
        validated = loader.load_config(str(CONFIG_PATH), non_interactive=True)
    except SystemExit as exc:  # _fatal() calls sys.exit(1)
        error = f"validation failed (exit {exc.code})"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {"ok": True, "data": raw, "valid": error is None, "error": error,
            "validated_keys": sorted(validated.keys()) if isinstance(validated, dict) else None}


def ep_config_save(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ApiError("body must be a JSON object", status=400)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    return {"ok": True}


def ep_target_get(_q: dict) -> dict:
    config = _read_config()
    tp = _target_path(config)
    if not tp.exists():
        raise ApiError(f"target file not found: {tp}", status=404)
    return {"ok": True, "path": str(tp), "code": tp.read_text(encoding="utf-8")}


def ep_project_model(_q: dict) -> dict:
    pm_mod = _imp("core.project_model")
    config = _read_config()
    pm = pm_mod.build_project_model(config)
    infos = []
    for mi in pm.all_module_infos():
        infos.append({
            "module": mi.dotted_name,
            "path": getattr(mi, "path", ""),
            "parsed": getattr(mi, "parsed_ok", getattr(mi, "ok", True)),
            "error": getattr(mi, "parse_error", getattr(mi, "err", "")) or "",
            "symbols": len(pm.symbols(mi.dotted_name)),
        })
    return {"ok": True, "root": pm.root, "modules": pm.modules(),
            "module_infos": infos}


def ep_project_symbols(q: dict) -> dict:
    module = (q.get("module") or [""])[0]
    if not module:
        raise ApiError("query param 'module' is required", status=400)
    pm_mod = _imp("core.project_model")
    pm = pm_mod.build_project_model(_read_config())
    syms = [{"qualified_name": s.qualified_name, "kind": s.kind,
             "lineno": s.lineno, "end_lineno": s.end_lineno}
            for s in pm.symbols(module)]
    return {"ok": True, "module": module, "symbols": syms}


def ep_graph_import(_q: dict) -> dict:
    pm = _imp("core.project_model").build_project_model(_read_config())
    g = pm.import_graph()
    return {"ok": True, "graph": g.to_dict(),
            "modules": g.modules(),
            "external_targets": g.external_targets()}


def ep_graph_call(_q: dict) -> dict:
    pm = _imp("core.project_model").build_project_model(_read_config())
    g = pm.call_graph()
    d = g.to_dict()
    return {"ok": True, "graph": d, "callers": g.callers(),
            "resolved": len(g.resolved_edges()),
            "unresolved": len(g.unresolved_edges())}


def ep_mutation_preview(body: dict) -> dict:
    config = _read_config()
    source = (body or {}).get("source")
    if not source:
        tp = _target_path(config)
        if not tp.exists():
            raise ApiError(f"target file not found: {tp}", status=404)
        source = tp.read_text(encoding="utf-8")
    mg = _imp("core.mutant_generator")
    try:
        mutants = mg.generate_all_mutants(source, config.get("mutation", {}))
    except SyntaxError as exc:
        raise ApiError(f"target source does not parse: {exc}", status=400)
    by_op: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    briefs = []
    for m in mutants:
        by_op[m.operator_name] = by_op.get(m.operator_name, 0) + 1
        by_cat[m.category] = by_cat.get(m.category, 0) + 1
        mut_line = ""
        lines = (m.code or "").splitlines()
        if 1 <= m.line <= len(lines):
            mut_line = lines[m.line - 1].strip()
        briefs.append({
            "id": m.id, "category": m.category, "operator": m.operator_name,
            "function": m.function_name, "line": m.line,
            "original": m.original_line_text.strip(), "mutated": mut_line,
            "original_op": m.original_op, "mutated_op": m.mutated_op,
        })
    return {"ok": True, "total": len(mutants),
            "by_operator": by_op, "by_category": by_cat, "mutants": briefs}


def ep_mutation_diff(q: dict) -> dict:
    mid = (q.get("id") or [""])[0]
    if not mid:
        raise ApiError("query param 'id' is required", status=400)
    config = _read_config()
    tp = _target_path(config)
    source = tp.read_text(encoding="utf-8")
    mg = _imp("core.mutant_generator")
    mutants = mg.generate_all_mutants(source, config.get("mutation", {}))
    for m in mutants:
        if m.id == mid:
            return {"ok": True, "id": m.id, "function": m.function_name,
                    "line": m.line, "original_full": source, "mutant_full": m.code}
    raise ApiError(f"mutant id not found in fresh generation: {mid}", status=404)


def _read_output_json(config: dict, name: str) -> dict:
    p = Path(_out_dir(config))
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    f = p / name
    if not f.exists():
        raise ApiError(f"{name} not found (run the pipeline first): {f}", status=404)
    with open(f, encoding="utf-8") as fh:
        return json.load(fh)


def ep_results_mutants(_q: dict) -> dict:
    return {"ok": True, "data": _read_output_json(_read_config(), "mutants.json")}


def ep_results_report(_q: dict) -> dict:
    return {"ok": True, "data": _read_output_json(_read_config(), "mutation_report.json")}


def ep_results_suite(_q: dict) -> dict:
    return {"ok": True, "data": _read_output_json(_read_config(), "minimum_test_suite.json")}


def ep_equivalence(_q: dict) -> dict:
    """Run provable-equivalence detection over the persisted mutants + target.

    Wires core.equivalence.detect_equivalent, which inspects getattr(m,'status'),
    getattr(m,'code'), m.id — so JSON dicts are wrapped as lightweight objects."""
    config = _read_config()
    data = _read_output_json(config, "mutants.json")
    if not isinstance(data, list):
        raise ApiError("mutants.json is not a list", status=400)
    objs = [SimpleNamespace(id=d.get("id"), status=d.get("status"),
                            code=d.get("code", "")) for d in data]
    tp = _target_path(config)
    original_source = tp.read_text(encoding="utf-8")
    eq = _imp("core.equivalence")
    ids = eq.detect_equivalent(objs, original_source)
    return {"ok": True, "method": "ast_identity_folding",
            "equivalent_ids": ids, "count": len(ids),
            "considered_suspected": sum(1 for o in objs if o.status == "SUSPECTED")}


def ep_history_trend(_q: dict) -> dict:
    ht = _imp("utils.history_tracker")
    config = _read_config()
    out = _out_dir(config)
    if not os.path.isabs(out):
        out = str(PROJECT_ROOT / out)
    hist = ht.load_history(out)
    return {"ok": True, "trend": ht.get_trend(hist), "runs": len(hist)}


def ep_history_runs(_q: dict) -> dict:
    ht = _imp("utils.history_tracker")
    config = _read_config()
    out = _out_dir(config)
    if not os.path.isabs(out):
        out = str(PROJECT_ROOT / out)
    return {"ok": True, "history": ht.load_history(out)}


def ep_export_pytest(_body: dict) -> dict:
    config = _read_config()
    out = _out_dir(config)
    if not os.path.isabs(out):
        out = str(PROJECT_ROOT / out)
    suite_path = os.path.join(out, "minimum_test_suite.json")
    if not os.path.exists(suite_path):
        raise ApiError("minimum_test_suite.json not found (run pipeline first)",
                       status=404)
    pe = _imp("utils.pytest_exporter")  # verified home: utils/pytest_exporter.py
    target = str(_target_path(config))
    path = pe.export_from_file(suite_path, target, project_root=str(PROJECT_ROOT))
    content = Path(path).read_text(encoding="utf-8")
    return {"ok": True, "path": path, "content": content}


def ep_backends(_q: dict) -> dict:
    """Report the VERIFIED execution backends (core.executor.make_executor)."""
    config = _read_config()
    current = (config.get("sandbox", {}) or {}).get(
        "execution_backend",
        (config.get("sandbox", {}) or {}).get("executor", "pool"))
    return {"ok": True, "current": current,
            "available": ["pool", "workerpool", "legacy", "container"],
            "note": "verified in core/executor.py make_executor(); 'container' "
                    "requires a docker/podman runtime on PATH."}


def ep_run(body: dict) -> dict:
    with _run_lock:
        if _run_state["running"]:
            raise ApiError("a run is already in progress", status=409)
    no_llm = bool((body or {}).get("no_llm", False))
    verbose = bool((body or {}).get("verbose", False))
    backend = (body or {}).get("backend") or None
    t = threading.Thread(target=_launch_pipeline,
                         args=(no_llm, verbose, backend), daemon=True)
    t.start()
    return {"ok": True}


def ep_run_stop(_body: dict) -> dict:
    with _run_lock:
        proc = _process
    if proc and proc.poll() is None:
        proc.kill()
        _log_queue.put({"type": "warn", "text": "⏹ stopped by user"})
        return {"ok": True}
    raise ApiError("no run in progress", status=409)


def ep_run_status(_q: dict) -> dict:
    with _run_lock:
        return {"ok": True, "data": dict(_run_state)}


def ep_capabilities(_q: dict) -> dict:
    """Honest capability map: what is wired vs. what has no backend (evidence)."""
    return {
        "ok": True,
        "wired": [
            {"feature": "Config view + REAL schema validation", "fn": "utils.config_loader.load_config(non_interactive=True)"},
            {"feature": "Reproducibility / environment", "fn": "infra.reproducibility.capture_env"},
            {"feature": "ProjectModel / module explorer", "fn": "core.project_model.build_project_model"},
            {"feature": "Symbol explorer", "fn": "ProjectModel.symbols(module)"},
            {"feature": "Import graph", "fn": "ProjectModel.import_graph().to_dict() / core.graphs.build_import_graph"},
            {"feature": "Call graph (best-effort)", "fn": "ProjectModel.call_graph().to_dict() / core.graphs.build_call_graph"},
            {"feature": "Pre-run mutation preview + operator/category stats + line diff", "fn": "core.mutant_generator.generate_all_mutants"},
            {"feature": "Mutant full-source diff", "fn": "MutantRecord.code vs target source"},
            {"feature": "Results: mutants / report / suite", "fn": "output/*.json (written by core.reporter.generate)"},
            {"feature": "Provable-equivalence detection", "fn": "core.equivalence.detect_equivalent"},
            {"feature": "Run history + trend", "fn": "utils.history_tracker.load_history/get_trend"},
            {"feature": "Pytest export", "fn": "utils.pytest_exporter.export_from_file"},
            {"feature": "Execution backend selection", "fn": "config.sandbox.execution_backend -> core.executor.make_executor"},
            {"feature": "Pipeline launch / stop / log stream", "fn": "subprocess python main.py (mirrors gui.py)"},
        ],
        "not_supported": [
            {"feature": "Pause / resume / restart a run", "evidence": "pipeline runs as one monolithic subprocess; no checkpoint/IPC; no pause hook anywhere"},
            {"feature": "Run only / skip a specific phase", "evidence": "no --phase CLI arg; argparse not present in provided main.py; orchestrator main module absent from audited copy"},
            {"feature": "Live worker / queue / thread telemetry", "evidence": "core.sandbox_executor._WorkerPool exposes no introspection API and runs inside the child process"},
            {"feature": "LLM token usage / cost tracking", "evidence": "llm.client.LLMClient.call returns only str; no token/cost accounting on its public surface"},
            {"feature": "Execution replay", "evidence": "no replay/recording capability in repo"},
            {"feature": "Experiment tracking / scenario templates / batch / scheduler", "evidence": "no such modules; only single-run history_tracker exists"},
            {"feature": "Multi-project management", "evidence": "config carries a single project; ProjectModel discovers one root only"},
            {"feature": "Mutant lineage", "evidence": "MutantRecord has no parent/lineage links; mutants are independent"},
            {"feature": "CPU / RAM / Disk live metrics", "evidence": "no telemetry in repo; would require psutil/proc (new dep) — omitted rather than faked"},
        ],
    }


# Route table: (method, path) -> (handler, takes_body)
GET_ROUTES = {
    "/api/health": ep_health,
    "/api/env": ep_env,
    "/api/config": ep_config_get,
    "/api/target": ep_target_get,
    "/api/project/model": ep_project_model,
    "/api/project/symbols": ep_project_symbols,
    "/api/graph/import": ep_graph_import,
    "/api/graph/call": ep_graph_call,
    "/api/mutation/diff": ep_mutation_diff,
    "/api/results/mutants": ep_results_mutants,
    "/api/results/report": ep_results_report,
    "/api/results/suite": ep_results_suite,
    "/api/results/equivalence": ep_equivalence,
    "/api/history/trend": ep_history_trend,
    "/api/history/runs": ep_history_runs,
    "/api/backends": ep_backends,
    "/api/run/status": ep_run_status,
    "/api/capabilities": ep_capabilities,
}
POST_ROUTES = {
    "/api/config": ep_config_save,
    "/api/mutation/preview": ep_mutation_preview,
    "/api/export/pytest": ep_export_pytest,
    "/api/run": ep_run,
    "/api/run/stop": ep_run_stop,
}


# ──────────────────────────────────────────────────────────────────────────────
# HTTP layer
# ──────────────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "V3ControlCenter/1.0"

    def log_message(self, *args) -> None:  # silence default access logging
        pass

    def _send_json(self, obj: dict, status: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self) -> None:
        if not HTML_PATH.exists():
            self._send_json({"ok": False, "error": "control_center.html missing"}, 500)
            return
        data = HTML_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send_html()
            return
        if path == "/api/run/stream":
            self._stream_logs()
            return
        handler = GET_ROUTES.get(path)
        if handler is None:
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        self._dispatch(handler, parse_qs(parsed.query))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        handler = POST_ROUTES.get(path)
        if handler is None:
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "invalid JSON body"}, 400)
            return
        self._dispatch(handler, body)

    def _dispatch(self, handler, arg) -> None:
        try:
            self._send_json(handler(arg))
        except ApiError as exc:
            self._send_json({"ok": False, "error": str(exc)}, exc.status)
        except Exception as exc:  # never leak a 500 without context
            tb = traceback.format_exc(limit=4)
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                             "trace": tb}, 500)

    def _stream_logs(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                try:
                    msg = _log_queue.get(timeout=20)
                except queue.Empty:
                    msg = {"type": "ping"}
                line = f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> None:
    ap = argparse.ArgumentParser(description="V3 Control Center (stdlib).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[V3 Control Center] http://{args.host}:{args.port}")
    print(f"[V3 Control Center] project root: {PROJECT_ROOT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[V3 Control Center] stopped")
        httpd.shutdown()


if __name__ == "__main__":
    main()
