"""
core/executor.py — Execution Abstraction (V3 prerequisite, M4.1)
================================================================
A stable execution seam that decouples the mutation pipeline from the concrete
M3 worker pool, so future backends (ContainerExecutor for I5, remote/distributed
executors) can be added WITHOUT changing the pipeline or the kill-matrix logic.

This layer is ADDITIVE and behavior-preserving:
  - The reference implementation remains `core.sandbox_executor._WorkerPool`
    (M3). `WorkerPoolExecutor` is a thin adapter around it — same jobs, same
    result tuples, same timeout/recycle semantics. Results are byte-identical to
    the direct-pool path.
  - Nothing here touches mutation scoring, ILP, the reporter, mutation
    semantics, timeout semantics, or the kill-matrix format.

Contract (the docx "Executor interface", §line 638):
    start()                      acquire workers / resources
    submit_many(jobs) -> results consume (code, input) jobs, return {job_id: result}
    shutdown()                   release resources

Jobs are `(job_id, code, func_name, input_json)` tuples (the exact shape the M3
pool consumes) or `ExecJob` instances (normalised to that tuple). Results are
`{job_id: (status, payload)}` where status/payload are produced verbatim by the
worker pool ("OK"/"TIMEOUT"/"CRASH"/"NO_FUNC").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class ExecJob:
    """One execution unit: run `func_name` from `code` on `input_json`.

    `job_id` is opaque to the executor and used only to key the result, so
    completion order never affects the caller (determinism preserved)."""
    job_id: Any
    code: str
    func_name: str
    input_json: str

    def as_tuple(self) -> tuple:
        return (self.job_id, self.code, self.func_name, self.input_json)


@runtime_checkable
class Executor(Protocol):
    """Execution backend contract. Implementations: WorkerPoolExecutor (M3,
    default); future ContainerExecutor (I5); future remote executors."""

    def start(self) -> None: ...

    def submit_many(self, jobs: Iterable) -> Dict[Any, tuple]:
        """Run all jobs; return {job_id: (status, payload)}. Order-independent."""
        ...

    def shutdown(self) -> None: ...


def _normalize(jobs: Iterable) -> List[tuple]:
    """Accept ExecJob or raw 4-tuples; return the raw tuples the pool consumes."""
    out: List[tuple] = []
    for j in jobs:
        out.append(j.as_tuple() if isinstance(j, ExecJob) else j)
    return out


class WorkerPoolExecutor:
    """Adapter over the M3 `_WorkerPool`. Byte-identical behavior to the direct
    pool path. The pool is the reference implementation and is left unchanged.

    A `pool_factory(max_workers, timeout)` may be injected (tests / future
    backends); by default the M3 pool is imported lazily to avoid an import
    cycle with `sandbox_executor`.
    """

    def __init__(
        self,
        max_workers: int,
        timeout: float,
        pool_factory: Optional[Callable[[int, float], Any]] = None,
    ) -> None:
        self._max_workers = max(1, int(max_workers))
        self._timeout = float(timeout)
        self._pool_factory = pool_factory
        self._pool: Any = None

    def start(self) -> None:
        if self._pool is None:
            factory = self._pool_factory
            if factory is None:
                # Lazy import → no module-load cycle with sandbox_executor.
                from core.sandbox_executor import _WorkerPool
                factory = _WorkerPool
            self._pool = factory(self._max_workers, self._timeout)

    def submit_many(self, jobs: Iterable) -> Dict[Any, tuple]:
        if self._pool is None:
            self.start()
        return self._pool.run(_normalize(jobs))

    def shutdown(self) -> None:
        if self._pool is not None:
            try:
                self._pool.shutdown()
            finally:
                self._pool = None

    def __enter__(self) -> "WorkerPoolExecutor":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.shutdown()
        return False


def make_executor(config: dict, max_workers: int, timeout: float) -> Executor:
    """Factory: select the execution backend from config.

    Canonical key `config.sandbox.execution_backend` (alias: `sandbox.executor`):
      - "pool" / "workerpool" (default) → WorkerPoolExecutor (M3 warm pool).
      - "legacy"                        → LegacyExecutor (spawn-per-exec via run_safe).
    Future: "container" (I5.4) — added here without touching callers.

    I5.3: both the warm-pool and the legacy spawn-per-exec paths now resolve to
    an Executor here, so execution flows through ONE interface
    (start/submit_many/shutdown). `execution_backend` is the canonical key
    (established in M3); `executor` is accepted as a backward-compatible alias.
    """
    sb = config.get("sandbox", {}) or {}
    backend = str(sb.get("execution_backend", sb.get("executor", "pool"))).lower()
    if backend in ("pool", "workerpool"):
        return WorkerPoolExecutor(max_workers, timeout)
    if backend == "legacy":
        return LegacyExecutor(max_workers, timeout)
    if backend == "container":
        # I5.4: container isolation backend. Lazy import keeps the runtime-
        # dependent code out of the import path until explicitly selected.
        from core.container_executor import ContainerExecutor
        return ContainerExecutor(max_workers, timeout, config)
    raise ValueError(f"unknown execution backend: {backend!r}")


class LegacyExecutor:
    """Adapter exposing the legacy spawn-per-exec path behind the Executor
    interface. Each job is run via `core.sandbox_executor.run_safe` (one fresh
    spawned process per execution), reproducing the pre-pool semantics exactly.

    Results are byte-identical to the M3/legacy reference for the same jobs;
    completion order is irrelevant (keyed by job_id). Provided so that
    `execution_backend="legacy"` flows through the same interface as the pool,
    removing the duplicated control flow in build_kill_matrix (I5.3)."""

    def __init__(self, max_workers: int, timeout: float) -> None:
        self._max_workers = max(1, int(max_workers))
        self._timeout = float(timeout)

    def start(self) -> None:
        pass

    def submit_many(self, jobs: Iterable) -> Dict[Any, tuple]:
        from core.sandbox_executor import run_safe
        import json as _json

        results: Dict[Any, tuple] = {}
        for job in _normalize(jobs):
            job_id, code, func_name, input_json = job
            inputs = _json.loads(input_json) if input_json else {}
            results[job_id] = run_safe(code, func_name, inputs, self._timeout)
        return results

    def shutdown(self) -> None:
        pass

    def __enter__(self) -> "LegacyExecutor":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.shutdown()
        return False
