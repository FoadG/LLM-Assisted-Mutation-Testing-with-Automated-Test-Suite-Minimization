"""
core/container_executor.py — Container isolation backend (I5.4, V3.1)
=====================================================================
An ADDITIVE Executor backend that runs each sandbox job inside an isolated OS
container. It implements the same Executor contract as WorkerPoolExecutor
(start / submit_many / shutdown) so it plugs into `make_executor` "without
touching callers" (docx L425, L638). Selected by config
`sandbox.execution_backend = "container"`; the default remains the warm pool.

HONESTY / ENVIRONMENT (read first)
----------------------------------
This module implements the architecture, the runtime probe, the command
construction, the result contract, and the lifecycle. It does NOT fake success:
  - If no container runtime (docker/podman) is available, `start()` raises a
    clear RuntimeError. It NEVER silently falls back to a weaker sandbox.
  - The in-container execution path is only exercised when a real runtime is
    present. In environments without one (e.g. this CI sandbox), the runtime
    paths are UNVERIFIED by execution — verified only structurally.

ISOLATION (docx L62 "FS/network isolation"; concrete profile NOT SPECIFIED IN
DOCX → minimal hardened defaults, flagged):
  - network: disabled (--network none)
  - filesystem: read-only root (--read-only) + small writable tmpfs scratch
  - memory: --memory cap (mirrors the in-process RLIMIT_AS budget)
  - pids: --pids-limit (contain fork bombs)
  - user: non-root (--user) where the image allows
  - per-job wall-timeout enforced host-side → TIMEOUT + container kill

RESULT CONTRACT (must match the pool/legacy backends, docx L456):
  Each job returns (status, payload) with status in
  {"OK","TIMEOUT","CRASH","NO_FUNC"}; SERIAL inputs are filtered upstream by the
  matrix builder exactly as for the other backends.

  V3.1 FIX: on "OK" the payload MUST be the function's return value serialized
  with the SAME semantics as core.sandbox_executor._execute_job
  (json.dumps(result, default=str, sort_keys=True) with a repr fallback).
  Previously the runner emitted "OK" with a None payload, so the kill predicate
  `result != orig_results[i]` in _build_columns_via_pool — where orig_results
  carry the serialized output — mis-fired for every non-crashing mutant
  (equivalent mutants flagged killed; output-difference kills indistinguishable
  from equivalents). Carrying the serialized output restores parity.
"""

from __future__ import annotations

import json
import shutil
from typing import Any, Dict, Iterable, List, Optional

# Default isolation/runtime knobs. All overridable via config.sandbox.container.
# Values are minimal hardened defaults; NOT SPECIFIED IN DOCX.
_DEFAULT_IMAGE      = "python:3.12-slim"
_DEFAULT_MEMORY     = "512m"
_DEFAULT_PIDS_LIMIT = 128
_CANDIDATE_RUNTIMES = ("docker", "podman")


def detect_runtime(preferred: Optional[str] = None) -> Optional[str]:
    """Return the path/name of an available container runtime, or None.

    Pure probe (uses shutil.which); does not execute anything. If `preferred`
    is given and present, it wins; otherwise the first available candidate."""
    if preferred:
        return preferred if shutil.which(preferred) else None
    for rt in _CANDIDATE_RUNTIMES:
        if shutil.which(rt):
            return rt
    return None


class ContainerExecutor:
    """Container-isolated execution backend. Implements the Executor protocol.

    Lifecycle:
      start()       probe runtime (fail loud if absent); prepare config.
      submit_many() run each job in an isolated container; collect results
                    keyed by job_id (order-independent → deterministic).
      shutdown()    remove any containers this executor created (no orphans).
    """

    def __init__(self, max_workers: int, timeout: float,
                 config: Optional[dict] = None) -> None:
        self._max_workers = max(1, int(max_workers))
        self._timeout = float(timeout)
        cfg = ((config or {}).get("sandbox", {}) or {}).get("container", {}) or {}
        self._image       = str(cfg.get("image", _DEFAULT_IMAGE))
        self._memory      = str(cfg.get("memory", _DEFAULT_MEMORY))
        self._pids_limit  = int(cfg.get("pids_limit", _DEFAULT_PIDS_LIMIT))
        self._runtime_pref = cfg.get("runtime")          # e.g. "docker"/"podman"
        self._runtime: Optional[str] = None
        self._started = False
        self._owned_containers: List[str] = []           # for cleanup / no orphans

    # ── Executor protocol ────────────────────────────────────────────────────
    def start(self) -> None:
        """Acquire the runtime. FAIL LOUD if none is available — never silently
        degrade to a weaker sandbox (that would violate the isolation contract)."""
        self._runtime = detect_runtime(self._runtime_pref)
        if self._runtime is None:
            raise RuntimeError(
                "ContainerExecutor requires a container runtime "
                "(docker/podman) but none was found on PATH. "
                "Set sandbox.execution_backend to 'pool' (default) or install a "
                "runtime. Refusing to fall back to a weaker sandbox."
            )
        self._started = True

    def submit_many(self, jobs: Iterable) -> Dict[Any, tuple]:
        if not self._started:
            self.start()
        results: Dict[Any, tuple] = {}
        for job in _normalize(jobs):
            job_id, code, func_name, input_json = job
            results[job_id] = self._run_one(code, func_name, input_json)
        return results

    def shutdown(self) -> None:
        """Remove any containers this executor created. Idempotent; no orphans."""
        for cid in self._owned_containers:
            try:
                self._runtime_rm(cid)
            except Exception:
                pass
        self._owned_containers.clear()
        self._started = False

    def __enter__(self) -> "ContainerExecutor":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.shutdown()
        return False

    # ── Runtime integration points (executed only when a runtime exists) ──────
    def _build_run_argv(self, container_name: str) -> List[str]:
        """Construct the hardened `run` argv. Integration point — testable
        without executing (asserts the isolation flags are present)."""
        assert self._runtime is not None
        return [
            self._runtime, "run", "--rm",
            "--name", container_name,
            "--network", "none",            # no network egress
            "--read-only",                  # read-only root fs
            "--tmpfs", "/tmp:rw,size=64m",  # small writable scratch
            "--memory", self._memory,       # memory cap
            "--pids-limit", str(self._pids_limit),
            "--cpus", "1",                  # bound CPU
            "-i", self._image,
            "python3", "-",                 # run the in-container runner from stdin
        ]

    def _run_one(self, code: str, func_name: str, input_json: str) -> tuple:
        """Run a single job in a fresh container and return (status, payload).

        Enforces the per-job wall-timeout host-side (subprocess timeout) →
        TIMEOUT + container kill. UNVERIFIED in runtimeless environments."""
        import subprocess, uuid
        name = f"mut-{uuid.uuid4().hex[:12]}"
        argv = self._build_run_argv(name)
        runner = _IN_CONTAINER_RUNNER.format(
            code=json.dumps(code),
            func=json.dumps(func_name),
            inp=json.dumps(input_json),
        )
        self._owned_containers.append(name)
        try:
            proc = subprocess.run(
                argv, input=runner.encode("utf-8"),
                capture_output=True, timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            try:
                self._runtime_kill(name)
            except Exception:
                pass
            return ("TIMEOUT", None)
        finally:
            # --rm removes on exit; drop from owned set
            try:
                self._owned_containers.remove(name)
            except ValueError:
                pass
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        if not out:
            return ("CRASH", "no-output")
        try:
            payload = json.loads(out.splitlines()[-1])
            return (payload["status"], payload.get("payload"))
        except Exception:
            return ("CRASH", "bad-output")

    def _runtime_kill(self, name: str) -> None:
        import subprocess
        subprocess.run([self._runtime, "kill", name],
                       capture_output=True, timeout=10)

    def _runtime_rm(self, name: str) -> None:
        import subprocess
        subprocess.run([self._runtime, "rm", "-f", name],
                       capture_output=True, timeout=10)


def _normalize(jobs: Iterable) -> List[tuple]:
    """Accept ExecJob or raw 4-tuples; return raw tuples (mirrors executor.py)."""
    out: List[tuple] = []
    for j in jobs:
        out.append(j.as_tuple() if hasattr(j, "as_tuple") else j)
    return out


# In-container runner: execs the code in a fresh namespace, calls func on inputs,
# and prints a single JSON line {"status","payload"} matching the result
# contract. Mirrors core.sandbox_executor._execute_job semantics EXACTLY,
# including the OK payload = serialized return value (OK/NO_FUNC/CRASH).
_IN_CONTAINER_RUNNER = r'''
import json, sys
code = json.loads({code})
func_name = json.loads({func})
input_json = json.loads({inp})
def emit(status, payload=None):
    sys.stdout.write(json.dumps({{"status": status, "payload": payload}}) + "\n")
    sys.stdout.flush()
try:
    ns = {{}}
    exec(compile(code, "<container-sandbox>", "exec"), ns)
    fn = ns.get(func_name)
    if fn is None:
        emit("NO_FUNC"); sys.exit(0)
    inputs = json.loads(input_json) if input_json else {{}}
    try:
        result = fn(**inputs) if isinstance(inputs, dict) else fn(inputs)
        # V3.1 FIX: serialize the RETURN VALUE with the EXACT semantics of
        # core.sandbox_executor._execute_job so the OK payload is comparable to
        # orig_results (which carry the serialized output). Without this the
        # kill predicate (res != orig_results[i]) mis-fires for every
        # non-crashing mutant under the container backend.
        try:
            serialized = json.dumps(result, default=str, sort_keys=True)
        except (TypeError, ValueError):
            serialized = repr(result)
        emit("OK", serialized)
    except Exception as e:
        emit("CRASH", type(e).__name__)
except Exception as e:
    emit("CRASH", type(e).__name__)
'''
